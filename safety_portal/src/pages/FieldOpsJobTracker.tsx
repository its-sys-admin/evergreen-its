import { useState, useEffect, useRef } from "react";
import type { FormEvent } from "react";
import * as api from "../lib/fieldops_jobtracker";
import { fetchPersonnelList, assignPersonnel, fetchMyCrew, type PersonnelRow, type MyCrewMember } from "../lib/fieldops_personnel";
import { fetchEquipmentList, moveEquipment } from "../lib/fieldops_equipment";
import { useAuth } from "../lib/auth";
import { PageShell } from "../components/PageShell";
import { SectionRail } from "../components/SectionRail";
import { useScrollSpy } from "../lib/useScrollSpy";
import { ChipX } from "../components/ChipX";
import { ExpectedMaterialsSection } from "../components/ExpectedMaterialsSection";
import { JobPaymentsCard } from "../components/JobPaymentsCard";
import { InlineRowMsg, SectionError, TaskDue, errMsg, type RowFeedback } from "../components/myTasksShared";
import { JobDailyRequirementsSection } from "../components/JobDailyRequirementsSection";
import { statusLabel, lifecycleLabel } from "../lib/labels";
import { JobArchivePanel } from "../components/JobArchivePanel";
import { formatJobNumber, splitJobNumber, JOB_NUMBER_MAX_LENGTH } from "../lib/jobNumber";
import { fmtHours, jobPillClass, loggedHours, openTaskCount, taskPillClass } from "../lib/jobtracker_view";

// R7 — a load-failure that owns a working Retry (never a dead banner, never a lying empty state).
interface RetryableError {
  text: string;
  retry: () => void;
}

// epoch SECONDS → ×1000 for JS Date
function fmtDateTime(epochSeconds: number | null): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

const STATUS_OPTIONS: { value: api.JobStatusFilter; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "closed", label: "Closed" },
  { value: "on_hold", label: "On hold" },
  { value: "all", label: "All" },
];

const MAX_CC = 5; // mirrors each Active-Jobs sheet's CC 1..5 columns (worker re-enforces)

// ── Routing SoR form block (P2.5 Slice 2) ────────────────────────────────────────────────────────
// The create form OWNS the full job source-of-truth; the contacts-edit form re-sends it. Both reuse
// <RoutingFields/>. Local form-state keeps every field a controlled string / string[]; routingPayload
// trims + drops empties so an UNTOUCHED routing block adds no keys to the create body (keeping the
// minimal-create contract byte-identical) and is the full intended routing for an edit.
interface RoutingForm {
  project_name: string;
  job_no: string;
  address: string;
  address_city: string;
  address_state: string;
  address_zip: string;
  stakeholder_name: string;
  stakeholder_email: string;
  stakeholder_phone: string;
  safety_contact_name: string;
  safety_contact_email: string;
  safety_cc: string[];
  progress_contact_name: string;
  progress_contact_email: string;
  progress_cc: string[];
}

const EMPTY_ROUTING: RoutingForm = {
  project_name: "",
  job_no: "",
  address: "",
  address_city: "",
  address_state: "",
  address_zip: "",
  stakeholder_name: "",
  stakeholder_email: "",
  stakeholder_phone: "",
  safety_contact_name: "",
  safety_contact_email: "",
  safety_cc: [],
  progress_contact_name: "",
  progress_contact_email: "",
  progress_cc: [],
};

function routingPayload(r: RoutingForm): api.JobRouting {
  const out: Record<string, unknown> = {};
  const scalars: [string, string][] = [
    ["project_name", r.project_name],
    ["job_no", r.job_no],
    ["address", r.address],
    ["address_city", r.address_city],
    ["address_state", r.address_state],
    ["address_zip", r.address_zip],
    ["stakeholder_name", r.stakeholder_name],
    ["stakeholder_email", r.stakeholder_email],
    ["stakeholder_phone", r.stakeholder_phone],
    ["safety_contact_name", r.safety_contact_name],
    ["safety_contact_email", r.safety_contact_email],
    ["progress_contact_name", r.progress_contact_name],
    ["progress_contact_email", r.progress_contact_email],
  ];
  for (const [k, v] of scalars) {
    const t = v.trim();
    if (t) out[k] = t;
  }
  const safetyCc = r.safety_cc.map((s) => s.trim()).filter(Boolean);
  const progressCc = r.progress_cc.map((s) => s.trim()).filter(Boolean);
  if (safetyCc.length) out.safety_cc = safetyCc;
  if (progressCc.length) out.progress_cc = progressCc;
  return out as api.JobRouting;
}

/** Seed the routing editor from the job detail's served routing block (0057) — the
 *  editor opens showing CURRENT values, so a save re-sends what's really there and an
 *  un-retyped field no longer silently wipes. */
function routingFormFromJob(job: api.JobDetail): RoutingForm {
  const r = job.routing;
  return {
    project_name: job.project_name ?? "",
    // 0064 — rejoin the stored (job_no, site_phase) pair into the full Evergreen
    // identifier. The editor shows what the operator typed ('2026.384.1'), not the split.
    job_no: formatJobNumber(job.job_no, job.site_phase),
    address: r?.address ?? "",
    address_city: r?.address_city ?? "",
    address_state: r?.address_state ?? "",
    address_zip: r?.address_zip ?? "",
    stakeholder_name: r?.stakeholder_name ?? "",
    stakeholder_email: r?.stakeholder_email ?? "",
    stakeholder_phone: r?.stakeholder_phone ?? "",
    safety_contact_name: r?.safety_contact_name ?? "",
    safety_contact_email: r?.safety_contact_email ?? "",
    safety_cc: r?.safety_cc ?? [],
    progress_contact_name: r?.progress_contact_name ?? "",
    progress_contact_email: r?.progress_contact_email ?? "",
    progress_cc: r?.progress_cc ?? [],
  };
}

// CC editor: up to MAX_CC email rows, each independently editable / removable. `contacts` (present
// only on the create form's SAFETY CC editor) turns each input into a suggest-or-free-text combobox
// via a <datalist> of configured delivery-contact EMAILS — free-text entry stays fully accepted (a
// datalist never restricts input). Progress CC (and every edit-form CcEditor) passes no contacts.
function CcEditor({
  label,
  ccs,
  onChange,
  contacts,
}: {
  label: string;
  ccs: string[];
  onChange: (next: string[]) => void;
  contacts?: api.DeliveryContact[];
}) {
  // Only contacts WITH an email are usable — the CC field holds an email, not a name.
  const suggestions = (contacts ?? []).filter((c) => c.email && c.email.trim());
  const listId = suggestions.length > 0 ? `${label.replace(/\s+/g, "-").toLowerCase()}-options` : undefined;
  return (
    <div className="dash-row" role="group" aria-label={label}>
      <span className="dash-card__label">{label} (≤{MAX_CC}):</span>{" "}
      {ccs.map((cc, i) => (
        <span key={i}>
          <input
            aria-label={`${label} ${i + 1}`}
            value={cc}
            placeholder="email@example.com"
            maxLength={320}
            list={listId}
            onChange={(e) => onChange(ccs.map((c, j) => (j === i ? e.target.value : c)))}
          />{" "}
          <ChipX
            ariaLabel={`Remove ${label} ${i + 1}`}
            onConfirm={() => onChange(ccs.filter((_, j) => j !== i))}
          />{" "}
        </span>
      ))}
      <button
        type="button"
        className="btn btn--secondary"
        aria-label={`Add ${label}`}
        disabled={ccs.length >= MAX_CC}
        onClick={() => onChange([...ccs, ""])}
      >
        + Add CC
      </button>
      {listId && (
        <datalist id={listId}>
          {suggestions.map((c) => (
            <option key={c.email} value={c.email!}>
              {c.name}
            </option>
          ))}
        </datalist>
      )}
    </div>
  );
}

// The full routing block: address, stakeholder, a Safety Reports block + a Progress Reports block
// (each contact name/email + CC editor), and a "Same as safety" copy button. After a copy the
// progress block stays INDEPENDENTLY editable (it's plain form state).
function RoutingFields({
  routing,
  onChange,
  showName = true,
  safetyCcContacts,
}: {
  routing: RoutingForm;
  onChange: (next: RoutingForm) => void;
  /** false on the CREATE form, which owns its dedicated Project-name field. */
  showName?: boolean;
  /** Delivery-contact suggestions for the SAFETY CC <datalist> — passed only by the create form.
   *  The Progress CC editor never receives it (safety-only, per the feature scope). */
  safetyCcContacts?: api.DeliveryContact[];
}) {
  const set = (patch: Partial<RoutingForm>) => onChange({ ...routing, ...patch });
  return (
    <>
      {showName && (
        <>
          <div className="dash-row">
            <input
              aria-label="Project name"
              value={routing.project_name}
              onChange={(e) => set({ project_name: e.target.value })}
              placeholder="Project name"
              maxLength={256}
            />
          </div>
          <p className="muted">
            Renaming a job changes which per-job folder future filings open (folders are keyed
            by name) — existing folders keep the old name.
          </p>
        </>
      )}
      <div className="dash-row">
        <input
          aria-label="Evergreen job number"
          value={routing.job_no}
          onChange={(e) => set({ job_no: e.target.value })}
          placeholder="Evergreen job # (YYYY.NNN or YYYY.NNN.S, e.g. 2026.384.1)"
          maxLength={JOB_NUMBER_MAX_LENGTH}
        />
      </div>
      {/* 0057 — structured address: street stays in `address`; city/state/zip are their own
          columns so the PO/RFQ ship-to autofill stops dumping everything into one line. */}
      <div className="dash-row">
        <input
          aria-label="Job street address"
          value={routing.address}
          onChange={(e) => set({ address: e.target.value })}
          placeholder="Street address (optional)"
          maxLength={512}
        />
      </div>
      <div className="dash-row">
        <input
          aria-label="Job city"
          value={routing.address_city}
          onChange={(e) => set({ address_city: e.target.value })}
          placeholder="City"
          maxLength={256}
        />{" "}
        <input
          aria-label="Job state"
          value={routing.address_state}
          onChange={(e) => set({ address_state: e.target.value.toUpperCase() })}
          placeholder="State (2-letter)"
          maxLength={2}
        />{" "}
        <input
          aria-label="Job ZIP"
          value={routing.address_zip}
          onChange={(e) => set({ address_zip: e.target.value })}
          placeholder="ZIP"
          maxLength={16}
        />
      </div>
      <fieldset className="dash-section" aria-label="Stakeholder">
        <legend className="dash-card__label">Stakeholder</legend>
        <div className="dash-row">
          <input value={routing.stakeholder_name} onChange={(e) => set({ stakeholder_name: e.target.value })} placeholder="Stakeholder name" maxLength={256} />{" "}
          <input value={routing.stakeholder_email} onChange={(e) => set({ stakeholder_email: e.target.value })} placeholder="Stakeholder email" maxLength={320} />{" "}
          <input value={routing.stakeholder_phone} onChange={(e) => set({ stakeholder_phone: e.target.value })} placeholder="Stakeholder phone" maxLength={40} />
        </div>
      </fieldset>
      <fieldset className="dash-section" aria-label="Safety Reports">
        <legend className="dash-card__label">Safety Reports</legend>
        <div className="dash-row">
          <button
            type="button"
            className="btn btn--secondary"
            onClick={() =>
              set({
                safety_contact_name: routing.stakeholder_name,
                safety_contact_email: routing.stakeholder_email,
              })
            }
          >
            Same as stakeholder
          </button>
        </div>
        <div className="dash-row">
          <input value={routing.safety_contact_name} onChange={(e) => set({ safety_contact_name: e.target.value })} placeholder="Safety contact name" maxLength={256} />{" "}
          <input value={routing.safety_contact_email} onChange={(e) => set({ safety_contact_email: e.target.value })} placeholder="Safety contact email" maxLength={320} />
        </div>
        <CcEditor label="Safety CC" ccs={routing.safety_cc} onChange={(safety_cc) => set({ safety_cc })} contacts={safetyCcContacts} />
      </fieldset>
      <fieldset className="dash-section" aria-label="Progress Reports">
        <legend className="dash-card__label">Progress Reports</legend>
        <div className="dash-row">
          <button
            type="button"
            className="btn btn--secondary"
            onClick={() =>
              set({
                progress_contact_name: routing.safety_contact_name,
                progress_contact_email: routing.safety_contact_email,
                progress_cc: [...routing.safety_cc],
              })
            }
          >
            Same as safety
          </button>
        </div>
        <div className="dash-row">
          <input value={routing.progress_contact_name} onChange={(e) => set({ progress_contact_name: e.target.value })} placeholder="Progress contact name" maxLength={256} />{" "}
          <input value={routing.progress_contact_email} onChange={(e) => set({ progress_contact_email: e.target.value })} placeholder="Progress contact email" maxLength={320} />
        </div>
        <CcEditor label="Progress CC" ccs={routing.progress_cc} onChange={(progress_cc) => set({ progress_cc })} />
      </fieldset>
    </>
  );
}

export function FieldOpsJobTracker({
  onBack,
  initialJobId,
  onJobViewChange,
  onOpenMaterials,
  onOpenSchedule,
  onOpenWeeklyReport,
}: {
  onBack: () => void;
  /** R7 — deep-link straight into a job's detail (the My Tasks "Log time" quick action / job-group
   *  links, routed through App). Consumed once on mount; absent → the normal list. */
  initialJobId?: string | null;
  /** G2.5 — reports USER-initiated list↔detail transitions up so App keeps the address bar
   *  shareable (/jobs/JOB-000018 ↔ /jobs). Deliberately NOT called for the initialJobId mount
   *  consumption (that navigation came FROM the URL — reporting it back would double-push).
   *  App syncs the URL only; this never re-renders or remounts the tracker. */
  onJobViewChange?: (jobId: string | null) => void;
  /** PR2 — deep link from the job detail's expected-materials section into the per-job Materials
   *  page. Absent (no cap.materials.receive) → the section renders without the button. */
  onOpenMaterials?: (jobId: string) => void;
  /** ADR-0006 PR-4 — deep link into the per-job Schedule page (all-roles view, decision 4).
   *  Absent (no cap.jobtracker.read — unreachable here in practice, since this page itself
   *  rides that cap) → the card doesn't render. */
  onOpenSchedule?: (jobId: string) => void;
  onOpenWeeklyReport?: (jobId: string) => void;
}) {
  const [view, setView] = useState<"list" | "detail">("list");
  const [jobs, setJobs] = useState<api.JobRow[]>([]);
  const [statusFilter, setStatusFilter] = useState<api.JobStatusFilter>("active");
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // R7 never-silent: list-area failures carry a working Retry (initial load / load-more / a failed
  // detail open all land here); reloadToken re-runs the list effect for the initial-load Retry.
  const [listError, setListError] = useState<RetryableError | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  // R7 — the viewer's own placement (worker viewer_current_job): the list badges "Your job".
  const [viewerCurrentJob, setViewerCurrentJob] = useState<string | null>(null);

  // Detail state
  const [selectedJob, setSelectedJob] = useState<api.JobDetail | null>(null);
  // R7 — the viewer's own linked roster row (worker-resolved), backing the "Me (<name>)" log-time
  // default. null = the account has no linked active personnel (the form says so explicitly).
  const [viewerPersonnel, setViewerPersonnel] = useState<api.ViewerPersonnel | null>(null);
  const [taskCursor, setTaskCursor] = useState<string | null>(null);
  const [timeCursor, setTimeCursor] = useState<string | null>(null);
  const [inspCursor, setInspCursor] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  // R7 never-silent: detail-area failures (leg load-more, post-mutation refresh) with Retry.
  const [detailError, setDetailError] = useState<RetryableError | null>(null);

  // Write (P2.3; Worker re-gates server-side — these caps drive UI affordances only).
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.jobtracker.manage"); // create / close / lifecycle / routing (admin)
  // D4: the per-job daily-form requirements editor (admin authoring — the Worker re-gates).
  const canChecklist = caps.includes("cap.checklist.manage");
  // Assigned-Tasks S1: task authority (create + reassign a task) is cap.jobtracker.manage OR
  // cap.tasks.assign, so a manager gets the add-task + per-task-assign controls (the Worker re-gates,
  // incl. the subcontractor-target guard). Job create / close / lifecycle / routing stay canManage-only.
  const canAssignTasks = canManage || caps.includes("cap.tasks.assign");
  const canOwnTasks = caps.includes("cap.tasks.own"); // change a task's own status
  const canLogTime = caps.includes("cap.time.log"); // log a time entry against the open job
  // Unified job-create flow: per-control caps (convenience — the Worker re-gates every call). A
  // manager (P2.6) holds crew.assign + equipment.field but NOT jobtracker.manage → can place crew /
  // move equipment on a job WITHOUT the add-task control (which stays under canManage).
  const canAssignCrew = caps.includes("cap.crew.assign"); // place / remove crew on this job
  const canFieldEquip = caps.includes("cap.equipment.field"); // move a piece of equipment to this job
  // Slice T: a SUBCONTRACTOR (logs time, but NOT a manager/admin — no cap.personnel.manage) may log
  // time only for THEMSELVES or crew THEY created. Their log-time picker offers that scoped list
  // (fetched below) rather than the job's full placed crew (which the Worker would 403 anyway).
  const isSubcontractor = canLogTime && !caps.includes("cap.personnel.manage");
  // R7 — mirror of the Worker's task-authority tiers (fieldops_task_write.ts), UI-convenience only:
  //   • assign-only (manager: cap.tasks.assign without cap.jobtracker.manage) — checkTaskTarget 403s
  //     any assign target whose linked account role isn't 'submitter' (no login → no role → 403),
  //     and checkTaskCurrentOwner 403s touching a task held by a non-submitter. Options disable.
  //   • own-only (cap.tasks.own without either authority cap) — checkTaskStatusOwnership 403s any
  //     task not assigned to the actor's own linked personnel. Status control renders only there.
  const isAssignOnlyActor = canAssignTasks && !canManage;
  const isOwnOnlyActor = canOwnTasks && !canAssignTasks;
  const [actionBusy, setActionBusy] = useState(false);
  const [actionMsg, setActionMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // R7 — per-task busy + inline feedback for the OPTIMISTIC status update (the R2 My-Tasks
  // pattern: apply locally, revert ONLY the acted-on row on failure, feedback lands on the row).
  const [taskBusyIds, setTaskBusyIds] = useState<ReadonlySet<number>>(new Set());
  const [taskRowMsgs, setTaskRowMsgs] = useState<Record<number, RowFeedback>>({});
  // New-job form (list view)
  const [newJobName, setNewJobName] = useState("");
  const [newJobClient, setNewJobClient] = useState("");
  // Client picker (2026-08-11). "" = no client · "new" = the free-text escape hatch · otherwise
  // the chosen client's id as a string. Defaults to "" so an untouched form still sends NO client
  // key at all, keeping the minimal-create contract byte-identical.
  const [newJobClientId, setNewJobClientId] = useState("");
  const [clientOptions, setClientOptions] = useState<api.ClientOption[]>([]);
  const [newJobOpen, setNewJobOpen] = useState(false);
  const [createRouting, setCreateRouting] = useState<RoutingForm>(EMPTY_ROUTING); // P2.5 routing SoR
  // Delivery-contact suggestions for the create form's Safety CC <datalist> (fetched when the form
  // opens). Best-effort: a load failure leaves it empty → no datalist, free-text CC entry still works.
  const [deliveryContacts, setDeliveryContacts] = useState<api.DeliveryContact[]>([]);
  // Detail manage controls
  const [taskDesc, setTaskDesc] = useState("");
  const [taskPerson, setTaskPerson] = useState(""); // add-task assignee (personnel id, "" = unassigned)
  const [taskDue, setTaskDue] = useState(""); // (G2.6) optional 'YYYY-MM-DD' deadline, "" = none
  // Detail lifecycle selector + routing/contacts editor (P2.5)
  const [lifecycleSel, setLifecycleSel] = useState<api.JobLifecycle>("active");
  const [editContactsOpen, setEditContactsOpen] = useState(false);
  const [editRouting, setEditRouting] = useState<RoutingForm>(EMPTY_ROUTING);
  // Time-log form (detail). logPerson holds the SUBJECT personnel id as a string; "" = the explicit
  // "Job-level (no person)" option. R7: the ambiguous "— me / unassigned —" default is gone — when
  // the viewer has a linked roster row the default is their own id ("Me (<name>)"); hoursError is
  // the inline client-side 0<h≤24 validation (the server independently 422s invalid_hours, R1).
  const [logHours, setLogHours] = useState("");
  const [hoursError, setHoursError] = useState<string | null>(null);
  const [logNotes, setLogNotes] = useState("");
  const [logTask, setLogTask] = useState("");
  const [logPerson, setLogPerson] = useState("");
  // G2.3 — non-destructive amend/void. amendTarget puts the log-time form in AMEND MODE (prefilled
  // from the row; submit → POST /time-entry/:uuid/amend, a NEW chain row — the old one is never
  // mutated). voidUuid opens the per-row required-reason input; timeRowBusy/timeRowMsgs are the
  // per-row busy + inline feedback (the R7 task-row pattern — never-silent, feedback on the row).
  const [amendTarget, setAmendTarget] = useState<api.JobTimeEntry | null>(null);
  const [voidUuid, setVoidUuid] = useState<string | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [timeRowBusy, setTimeRowBusy] = useState<string | null>(null);
  const [timeRowMsgs, setTimeRowMsgs] = useState<Record<string, RowFeedback>>({});
  // Unified job-create flow (Slice 2/3): assign-crew + assign-equipment pickers on the detail view,
  // plus the one-shot "finish setting up" nudge shown when a create routes into the new job's detail.
  const [crewOpts, setCrewOpts] = useState<PersonnelRow[]>([]);
  const [equipOpts, setEquipOpts] = useState<{ id: number; name: string; identifier: string | null }[]>([]);
  const [crewToAdd, setCrewToAdd] = useState("");
  const [equipToAdd, setEquipToAdd] = useState("");
  const [setupBanner, setSetupBanner] = useState<string | null>(null);
  // Slice T — a subcontractor's own loggable crew (self + created), for the time-log person picker.
  const [myCrew, setMyCrew] = useState<MyCrewMember[]>([]);
  // R7 never-silent: picker/myCrew load failures surface with Retry instead of a silent empty list.
  const [pickerError, setPickerError] = useState<string | null>(null);
  const [myCrewError, setMyCrewError] = useState<string | null>(null);

  // Reload the list whenever the status filter changes (and on mount); reloadToken re-runs it for
  // the error-block Retry.
  useEffect(() => {
    let live = true;
    setLoading(true);
    setListError(null);
    api
      .fetchJobList(statusFilter)
      .then((data) => {
        if (!live) return;
        setJobs(data.jobs);
        setCursor(data.next_cursor);
        setViewerCurrentJob(data.viewer_current_job ?? null);
      })
      .catch(
        () =>
          live &&
          setListError({ text: "Failed to load jobs.", retry: () => setReloadToken((t) => t + 1) }),
      )
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [statusFilter, reloadToken]);

  // Load the crew + equipment pickers when the detail view is open and the actor can assign. These
  // are the full active roster / equipment set (not job-scoped); reloaded after each mutation so the
  // "already placed here" exclusion stays current. R7 never-silent: a load failure keeps the control
  // rendered on the previous options AND surfaces a visible error with Retry (it was a silent
  // catch-into-empty — A4 swallow site 3). It still never blocks the detail view.
  async function reloadPickers() {
    const failed: string[] = [];
    if (canAssignCrew) {
      try {
        setCrewOpts((await fetchPersonnelList()).personnel);
      } catch {
        failed.push("crew");
      }
    }
    if (canFieldEquip) {
      try {
        const r = await fetchEquipmentList();
        setEquipOpts(r.equipment.map((eq) => ({ id: eq.id, name: eq.name, identifier: eq.identifier })));
      } catch {
        failed.push("equipment");
      }
    }
    setPickerError(failed.length ? `Couldn't load the ${failed.join(" and ")} picker options.` : null);
  }

  useEffect(() => {
    if (view === "detail" && selectedJob && (canAssignCrew || canFieldEquip)) void reloadPickers();
    // reloadPickers is recreated each render and reads the current caps; job_id keys the reload.
  }, [view, selectedJob?.job_id, canAssignCrew, canFieldEquip]);

  // Slice T — load a subcontractor's own loggable crew (self + created) for the time-log picker.
  // R7 never-silent: failure surfaces next to the log-time form with Retry (A4 swallow site 4).
  function loadMyCrew() {
    fetchMyCrew()
      .then((m) => {
        setMyCrew(m);
        setMyCrewError(null);
      })
      .catch(() => setMyCrewError("Couldn't load your crew for the time log."));
  }
  useEffect(() => {
    if (view === "detail" && selectedJob && isSubcontractor) loadMyCrew();
  }, [view, selectedJob?.job_id, isSubcontractor]);

  // Load the delivery-contact suggestions when the create form opens (only the create form's Safety
  // CC field uses them). Best-effort — a failure leaves the list empty so the field degrades to
  // plain free-text (no datalist), never blocking job creation.
  useEffect(() => {
    if (!newJobOpen || !canManage) return;
    let live = true;
    api
      .getDeliveryContacts()
      .then((cs) => live && setDeliveryContacts(cs))
      .catch(() => {
        /* best-effort: datalist absent, free-text CC entry still works */
      });
    // The client picker's options, fetched on the same trigger. Also best-effort, and the
    // degraded state is deliberately SAFE: an empty list leaves only "No client" and
    // "+ New client…", so a failed load can never silently bind a job to the WRONG existing
    // client — the worst case is the operator typing a name, which the Worker then
    // find-or-creates onto the right row anyway (§45).
    api
      .fetchClients()
      .then((cs) => live && setClientOptions(cs))
      .catch(() => {
        /* best-effort: picker shows only "No client" / "+ New client…" */
      });
    return () => {
      live = false;
    };
  }, [newJobOpen, canManage]);

  async function loadMore() {
    if (!cursor || loading) return;
    setLoading(true);
    try {
      const data = await api.fetchJobList(statusFilter, cursor);
      setJobs((prev) => [...prev, ...data.jobs]);
      setCursor(data.next_cursor);
      setListError(null);
    } catch {
      setListError({ text: "Failed to load more jobs.", retry: () => void loadMore() });
    } finally {
      setLoading(false);
    }
  }

  // Open a job's detail by id (card click, deep-link, retry, post-create). R7: a failed detail
  // fetch RETURNS to the list with an error+Retry — the old path stranded the view mid-open with
  // the failure banner rendered only on the (hidden) list (A4 swallow adjacent).
  function openJobById(jobId: string) {
    setView("detail");
    setSelectedJob(null);
    setViewerPersonnel(null);
    setDetailLoading(true);
    setDetailError(null);
    setEditContactsOpen(false);
    setEditRouting(EMPTY_ROUTING);
    setSetupBanner(null); // opening an existing job from the list is not the create-nudge path
    setCrewToAdd("");
    setEquipToAdd("");
    setTaskRowMsgs({});
    setHoursError(null);
    // G2.3 — a stale amend/void context must never survive a job switch.
    setAmendTarget(null);
    setVoidUuid(null);
    setVoidReason("");
    setTimeRowMsgs({});
    setLogHours("");
    setLogNotes("");
    setLogTask("");
    api
      .fetchJobDetail(jobId)
      .then((res) => {
        setSelectedJob(res.job);
        setViewerPersonnel(res.viewer_personnel ?? null);
        // R7 — the log-time subject defaults to the viewer's OWN roster row when linked ("Me").
        setLogPerson(res.viewer_personnel ? String(res.viewer_personnel.id) : "");
        // Seed the lifecycle selector from the job's OWN lifecycle field. This used to derive from
        // the legacy `status` ("active" → 'active', anything else → 'inactive') because the detail
        // payload carried no lifecycle — and since `status` collapses inactive AND archived into
        // 'closed', an ARCHIVED job silently re-displayed as "Inactive" on every reload. The
        // runbook had to instruct operators to "validate by effects, not the dropdown". The worker
        // now sends `lifecycle`, so the selector can simply tell the truth.
        setLifecycleSel(res.job.lifecycle);
        setTaskCursor(res.cursors.tasks);
        setTimeCursor(res.cursors.time);
        setInspCursor(res.cursors.insp);
        setDetailLoading(false);
      })
      .catch(() => {
        setView("list");
        setDetailLoading(false);
        setListError({ text: "Failed to load job details.", retry: () => openJobById(jobId) });
      });
  }

  function handleCardClick(job: api.JobRow) {
    onJobViewChange?.(job.job_id); // G2.5: App pushes /jobs/<id> — the texted-link moment
    openJobById(job.job_id);
  }

  // R7 — deep-link consumption: mount with an initialJobId → open that job's detail directly.
  const deepLinked = useRef(false);
  useEffect(() => {
    if (initialJobId && !deepLinked.current) {
      deepLinked.current = true;
      openJobById(initialJobId);
    }
    // Mount-only by design (the prop is a one-shot deep link, keyed by App on change).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleBack() {
    if (view === "detail") {
      onJobViewChange?.(null); // G2.5: App REPLACES the URL back to /jobs (dismiss, not deeper)
      setView("list");
      setSelectedJob(null);
      setViewerPersonnel(null);
      setTaskCursor(null);
      setTimeCursor(null);
      setInspCursor(null);
      setListError(null);
      setDetailError(null);
      setSetupBanner(null);
      setTaskRowMsgs({});
    } else {
      onBack();
    }
  }

  // Each history leg (tasks / time / inspections) paginates INDEPENDENTLY: a "Load more" on one
  // leg re-fetches the detail with only that leg's cursor and appends just that leg's new rows,
  // leaving the others untouched. The worker returns a fresh { tasks, time, insp } cursor set.
  async function loadMoreLeg(leg: "task" | "time" | "insp") {
    if (!selectedJob) return;
    const cur = leg === "task" ? taskCursor : leg === "time" ? timeCursor : inspCursor;
    if (!cur) return;
    setDetailLoading(true);
    try {
      const res = await api.fetchJobDetail(selectedJob.job_id, { [leg]: cur });
      setSelectedJob((prev) => {
        if (!prev) return res.job;
        if (leg === "task") return { ...prev, tasks: [...prev.tasks, ...res.job.tasks] };
        if (leg === "time") return { ...prev, time_entries: [...prev.time_entries, ...res.job.time_entries] };
        return { ...prev, inspections: [...prev.inspections, ...res.job.inspections] };
      });
      if (leg === "task") setTaskCursor(res.cursors.tasks);
      else if (leg === "time") setTimeCursor(res.cursors.time);
      else setInspCursor(res.cursors.insp);
      setDetailError(null);
    } catch {
      // R7: this error used to land in the LIST-only banner — invisible from the open detail
      // (A4 swallow adjacent). It now renders in the detail with a working Retry.
      setDetailError({ text: "Failed to load more.", retry: () => void loadMoreLeg(leg) });
    } finally {
      setDetailLoading(false);
    }
  }

  function LoadMoreBtn({ leg }: { leg: "task" | "time" | "insp" }) {
    return (
      <div className="dash-row dash-load-more">
        <button onClick={() => loadMoreLeg(leg)} disabled={detailLoading} className="btn btn--secondary">
          {detailLoading ? "Loading..." : "Load more"}
        </button>
      </div>
    );
  }

  // ── WRITE handlers (P2.3) ──────────────────────────────────────────────────────────────────────
  // Re-fetch the open job from scratch (drops appended history legs back to the first page; the
  // mutation just landed so the first page is what we want to show). Resets all three leg cursors.
  async function reloadDetail() {
    if (!selectedJob) return;
    const res = await api.fetchJobDetail(selectedJob.job_id);
    setSelectedJob(res.job);
    setViewerPersonnel(res.viewer_personnel ?? null);
    setTaskCursor(res.cursors.tasks);
    setTimeCursor(res.cursors.time);
    setInspCursor(res.cursors.insp);
  }

  // R7 — post-mutation refresh, TRY-SPLIT from the mutation itself (the R2 standard): the mutation
  // LANDED, so a refetch hiccup must never flip the success message into a failure — the on-screen
  // data just goes stale, and this says so with a working Retry.
  async function refreshDetailAfterMutation() {
    try {
      await reloadDetail();
      setDetailError(null);
    } catch {
      setDetailError({
        text: "Saved, but refreshing the job failed — the view may be out of date.",
        retry: () => void refreshDetailAfterMutation(),
      });
    }
  }

  async function reloadList() {
    const data = await api.fetchJobList(statusFilter);
    setJobs(data.jobs);
    setCursor(data.next_cursor);
  }

  async function submitNewJob(e: FormEvent) {
    e.preventDefault();
    if (actionBusy) return;
    const projectName = newJobName.trim();
    if (!projectName) {
      setActionMsg({ ok: false, text: "Project name is required." });
      return;
    }
    // TOMBSTONE (operator decision, 2026-08-11): the "At least one Safety CC recipient is
    // required." create-time block was REMOVED here and in the Worker (fieldops_job_write.ts).
    // Safety CC is now optional at create — a job may be created with none and have recipients
    // added later through the routing editor, which always permitted an empty list. The ≤MAX_CC
    // cap and the per-entry email-shape check are UNCHANGED (CcEditor + the Worker's parseCc).
    //
    // Validate the Evergreen number before submitting so a typo is a pointed inline message
    // rather than a round-trip 400 — the Worker still re-validates and remains authoritative.
    if (splitJobNumber(createRouting.job_no) === null) {
      setActionMsg({
        ok: false,
        text: "The Evergreen job number must look like 2026.384 or 2026.384.1 (year.number.site).",
      });
      return;
    }
    setActionBusy(true);
    setActionMsg(null);
    try {
      const clientName = newJobClient.trim();
      // Client linkage. An EXISTING pick sends client_id (the row is reused, never duplicated);
      // "new" sends new_client. The two are mutually exclusive — the Worker 400s on both, and it
      // now find-or-creates by name anyway, so a name that already exists reuses its row even
      // through this branch.
      const clientLink =
        newJobClientId && newJobClientId !== "new"
          ? { client_id: Number(newJobClientId) }
          : newJobClientId === "new" && clientName
            ? { new_client: { name: clientName } }
            : {};
      // Slice 6: no job_id in the body — the worker assigns the next JOB-###### and returns it.
      const created = await api.createJob({
        project_name: projectName,
        ...clientLink,
        ...routingPayload(createRouting),
      });
      setNewJobName("");
      setNewJobClient("");
      setNewJobClientId("");
      setCreateRouting(EMPTY_ROUTING);
      setNewJobOpen(false);
      await reloadList();
      // Slice 3: route into the new job's detail with a one-shot "finish setting up" nudge so the
      // office immediately assigns crew / equipment / tasks. If the detail fetch fails, stay on the
      // list — the job is created and the success toast still shows, and (R7, A4 swallow site 5)
      // the failed open is SAID OUT LOUD with a Retry instead of silently dumping back to the list.
      try {
        const res = await api.fetchJobDetail(created.job_id);
        setSelectedJob(res.job);
        setViewerPersonnel(res.viewer_personnel ?? null);
        setLogPerson(res.viewer_personnel ? String(res.viewer_personnel.id) : "");
        setLifecycleSel(res.job.status === "active" ? "active" : "inactive");
        setTaskCursor(res.cursors.tasks);
        setTimeCursor(res.cursors.time);
        setInspCursor(res.cursors.insp);
        setEditContactsOpen(false);
        setEditRouting(EMPTY_ROUTING);
        setCrewToAdd("");
        setEquipToAdd("");
        setSetupBanner(created.job_id);
        setView("detail");
      } catch {
        // The job WAS created — only the detail open failed. Stay on the list, say so, offer Retry.
        setListError({
          text: `Job ${created.job_id} was created, but opening it failed.`,
          retry: () => openJobById(created.job_id),
        });
      }
      setActionMsg({ ok: true, text: `Job ${created.job_id} created.` });
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Create failed." });
    } finally {
      setActionBusy(false);
    }
  }

  // Lifecycle selector (P2.5) — replaces the bare Close button. Setting it explicitly persists the
  // chosen value through the reload (which would otherwise re-derive only active/inactive from status).
  // R7: every handler below TRY-SPLITS mutation vs refetch (refreshDetailAfterMutation) — a landed
  // mutation keeps its success message even when the follow-up refetch fails.
  async function submitLifecycle(lifecycle: api.JobLifecycle) {
    if (!selectedJob || actionBusy) return;
    setActionBusy(true);
    setActionMsg(null);
    try {
      await api.setLifecycle(selectedJob.job_id, lifecycle);
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Lifecycle update failed." });
      setActionBusy(false);
      return;
    }
    setLifecycleSel(lifecycle);
    setActionMsg({ ok: true, text: `Lifecycle set to ${lifecycle}.` });
    await refreshDetailAfterMutation();
    setActionBusy(false);
  }

  async function submitEditContacts(e: FormEvent) {
    e.preventDefault();
    if (!selectedJob || actionBusy) return;
    // Same pre-flight as create — the edit form carries the identical Evergreen-number input,
    // so a typo here deserves the identical pointed message instead of a bare 400.
    if (splitJobNumber(editRouting.job_no) === null) {
      setActionMsg({
        ok: false,
        text: "The Evergreen job number must look like 2026.384 or 2026.384.1 (year.number.site).",
      });
      return;
    }
    setActionBusy(true);
    setActionMsg(null);
    try {
      await api.editContacts(selectedJob.job_id, routingPayload(editRouting));
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Routing update failed." });
      setActionBusy(false);
      return;
    }
    setEditContactsOpen(false);
    setEditRouting(EMPTY_ROUTING);
    setActionMsg({ ok: true, text: "Routing / contacts updated." });
    await refreshDetailAfterMutation();
    setActionBusy(false);
  }

  async function submitAddTask(e: FormEvent) {
    e.preventDefault();
    if (!selectedJob || actionBusy) return;
    const description = taskDesc.trim();
    if (!description) {
      setActionMsg({ ok: false, text: "Task description is required." });
      return;
    }
    const personnelId = taskPerson === "" ? undefined : Number(taskPerson);
    setActionBusy(true);
    setActionMsg(null);
    try {
      await api.addTask(selectedJob.job_id, {
        description,
        ...(personnelId !== undefined ? { personnel_id: personnelId } : {}),
        // (G2.6) optional deadline — any calendar date (no max: future dates fine, past dates
        // legal for an already-late task); the Worker shape-validates.
        ...(taskDue !== "" ? { due_date: taskDue } : {}),
      });
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Add task failed." });
      setActionBusy(false);
      return;
    }
    setTaskDesc("");
    setTaskPerson("");
    setTaskDue("");
    setActionMsg({ ok: true, text: "Task added." });
    await refreshDetailAfterMutation();
    setActionBusy(false);
  }

  // (Re)assign or clear a task's assignee (task authority). Options are the job's placed crew;
  // R7: ineligible options are pre-disabled for an assign-only manager (see assignOptionState).
  async function submitReassignTask(taskId: number, personId: number | null) {
    if (!selectedJob || actionBusy) return;
    setActionBusy(true);
    setActionMsg(null);
    try {
      await api.reassignTask(taskId, personId);
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Reassign failed." });
      setActionBusy(false);
      return;
    }
    setActionMsg({ ok: true, text: personId === null ? "Task unassigned." : "Task reassigned." });
    await refreshDetailAfterMutation();
    setActionBusy(false);
  }

  // R7 — OPTIMISTIC per-row status change (the R2 My-Tasks pattern): apply locally, revert ONLY
  // this row on failure, per-row busy + inline feedback. No whole-detail refetch — the applied
  // local state IS the mutation's result.
  function setTaskRowMsg(id: number, msg: RowFeedback | null) {
    setTaskRowMsgs((m) => {
      const next = { ...m };
      if (msg) next[id] = msg;
      else delete next[id];
      return next;
    });
  }

  async function changeTaskStatus(taskId: number, status: api.TaskStatus) {
    if (taskBusyIds.has(taskId)) return;
    setTaskBusyIds((s) => new Set(s).add(taskId));
    setTaskRowMsg(taskId, null);
    const prevStatus = selectedJob?.tasks.find((t) => t.id === taskId)?.status;
    setSelectedJob((j) =>
      j ? { ...j, tasks: j.tasks.map((t) => (t.id === taskId ? { ...t, status } : t)) } : j,
    );
    try {
      await api.setTaskStatus(taskId, status);
      setTaskRowMsg(taskId, { ok: true, text: "Updated." });
    } catch (err) {
      if (prevStatus !== undefined) {
        setSelectedJob((j) =>
          j ? { ...j, tasks: j.tasks.map((t) => (t.id === taskId ? { ...t, status: prevStatus } : t)) } : j,
        );
      }
      setTaskRowMsg(taskId, { ok: false, text: errMsg(err, "Update failed.") });
    } finally {
      setTaskBusyIds((s) => {
        const next = new Set(s);
        next.delete(taskId);
        return next;
      });
    }
  }

  async function submitLogTime(e: FormEvent) {
    e.preventDefault();
    if (!selectedJob || actionBusy) return;
    // R7 — hours is REQUIRED, 0 < h ≤ 24, validated inline (the server independently 422s
    // invalid_hours per R1; this is the client half of the same bound). Voiding (hours = 0) rides
    // the per-row Void control, not this form, so the Edit path keeps the same lower bound.
    const hours = Number(logHours);
    if (logHours.trim() === "" || !Number.isFinite(hours) || hours <= 0 || hours > 24) {
      setHoursError("Enter the hours worked — more than 0, at most 24 (e.g. 7.5).");
      return;
    }
    setHoursError(null);
    const taskId = logTask === "" ? undefined : Number(logTask);
    const personnelId = logPerson === "" ? undefined : Number(logPerson);
    setActionBusy(true);
    setActionMsg(null);
    try {
      if (amendTarget) {
        // G2.3 AMEND — a NEW chain row replacing the target as head (the Worker inherits job_id +
        // submitted_as from the target and re-gates recorder-or-manager). Full-replacement body;
        // the untouched-in-form event-time claims carry over so a correction can't silently shift
        // the entry across a rollup week (work_started_at windows the labor sum).
        await api.amendTimeEntry(amendTarget.uuid, {
          uuid: crypto.randomUUID(), // client-generated idempotency key (integrity-bar)
          hours,
          ...(taskId !== undefined ? { task_id: taskId } : {}),
          ...(personnelId !== undefined ? { personnel_id: personnelId } : {}),
          ...(logNotes.trim() ? { notes: logNotes.trim() } : {}),
          ...(amendTarget.work_started_at !== null ? { work_started_at: amendTarget.work_started_at } : {}),
          ...(amendTarget.work_ended_at !== null ? { work_ended_at: amendTarget.work_ended_at } : {}),
        });
      } else {
        await api.logTime({
          uuid: crypto.randomUUID(), // client-generated idempotency key (integrity-bar)
          job_id: selectedJob.job_id,
          hours,
          ...(taskId !== undefined ? { task_id: taskId } : {}),
          ...(personnelId !== undefined ? { personnel_id: personnelId } : {}),
          ...(logNotes.trim() ? { notes: logNotes.trim() } : {}),
        });
      }
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Time log failed." });
      setActionBusy(false);
      return;
    }
    const wasAmend = amendTarget !== null;
    resetTimeForm();
    setActionMsg({ ok: true, text: wasAmend ? "Entry corrected." : "Time logged." });
    await refreshDetailAfterMutation();
    setActionBusy(false);
  }

  // ── G2.3 — amend/void helpers ────────────────────────────────────────────────────────────────────
  function resetTimeForm() {
    setAmendTarget(null);
    setLogHours("");
    setLogNotes("");
    setLogTask("");
    setLogPerson(viewerPersonnel ? String(viewerPersonnel.id) : ""); // back to the Me default
    setHoursError(null);
  }

  /** Put the log-time form into AMEND MODE, prefilled from the row being corrected. */
  function startAmend(t: api.JobTimeEntry) {
    setAmendTarget(t);
    setVoidUuid(null);
    setVoidReason("");
    setLogHours(t.hours !== null ? String(t.hours) : "");
    setLogNotes(t.notes ?? "");
    setLogTask(t.task_id !== null ? String(t.task_id) : "");
    setLogPerson(t.personnel_id !== null ? String(t.personnel_id) : "");
    setHoursError(null);
  }

  function setTimeRowMsg(uuid: string, msg: RowFeedback | null) {
    setTimeRowMsgs((prev) => {
      const next = { ...prev };
      if (msg === null) delete next[uuid];
      else next[uuid] = msg;
      return next;
    });
  }

  /** VOID = an amend with hours 0 + the REQUIRED reason. Carries the row's subject/task/event-time
   *  over so the struck-through head still says who/what it was about. Per-row busy + inline
   *  feedback (never-silent); the Worker re-gates (recorder-or-manager, head-only). */
  async function submitVoid(t: api.JobTimeEntry) {
    const reason = voidReason.trim();
    if (reason === "") {
      setTimeRowMsg(t.uuid, { ok: false, text: "Add a short reason to void this entry." });
      return;
    }
    if (timeRowBusy !== null) return;
    setTimeRowBusy(t.uuid);
    setTimeRowMsg(t.uuid, null);
    try {
      await api.amendTimeEntry(t.uuid, {
        uuid: crypto.randomUUID(),
        hours: 0,
        notes: reason,
        ...(t.task_id !== null ? { task_id: t.task_id } : {}),
        ...(t.personnel_id !== null ? { personnel_id: t.personnel_id } : {}),
        ...(t.work_started_at !== null ? { work_started_at: t.work_started_at } : {}),
        ...(t.work_ended_at !== null ? { work_ended_at: t.work_ended_at } : {}),
      });
    } catch (err) {
      setTimeRowMsg(t.uuid, { ok: false, text: err instanceof Error ? err.message : "Void failed." });
      setTimeRowBusy(null);
      return;
    }
    setVoidUuid(null);
    setVoidReason("");
    setTimeRowBusy(null);
    setActionMsg({ ok: true, text: "Entry voided." });
    await refreshDetailAfterMutation();
  }

  // ── Unified job-create flow: assign crew + equipment to the open job (Slice 2) ───────────────────
  // Reuse the P2.6 assignPersonnel (cap.crew.assign) and the equipment move (cap.equipment.field)
  // routes — already security-reviewed. Each reloads the detail (crew / equipment_on_site reflect
  // the change via the Slice-1 crew-convergence + the equipment-on-site leg) and the pickers.
  async function submitAssignCrew(e: FormEvent) {
    e.preventDefault();
    if (!selectedJob || actionBusy) return;
    const pid = Number(crewToAdd);
    if (crewToAdd === "" || !Number.isInteger(pid)) {
      setActionMsg({ ok: false, text: "Select a crew member to place on this job." });
      return;
    }
    setActionBusy(true);
    setActionMsg(null);
    try {
      await assignPersonnel(pid, selectedJob.job_id);
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Assign failed." });
      setActionBusy(false);
      return;
    }
    setCrewToAdd("");
    setActionMsg({ ok: true, text: "Crew member placed on this job." });
    await refreshDetailAfterMutation();
    await reloadPickers();
    setActionBusy(false);
  }

  async function removeCrew(personId: number) {
    if (!selectedJob || actionBusy) return;
    setActionBusy(true);
    setActionMsg(null);
    try {
      await assignPersonnel(personId, null); // clear the placement (unassign)
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Remove failed." });
      setActionBusy(false);
      return;
    }
    setActionMsg({ ok: true, text: "Crew member removed from this job." });
    await refreshDetailAfterMutation();
    await reloadPickers();
    setActionBusy(false);
  }

  async function submitAssignEquip(e: FormEvent) {
    e.preventDefault();
    if (!selectedJob || actionBusy) return;
    const eid = Number(equipToAdd);
    if (equipToAdd === "" || !Number.isInteger(eid)) {
      setActionMsg({ ok: false, text: "Select a piece of equipment to move to this job." });
      return;
    }
    setActionBusy(true);
    setActionMsg(null);
    try {
      await moveEquipment(eid, { job_id: selectedJob.job_id });
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Move failed." });
      setActionBusy(false);
      return;
    }
    setEquipToAdd("");
    setActionMsg({ ok: true, text: "Equipment moved to this job." });
    await refreshDetailAfterMutation();
    await reloadPickers();
    setActionBusy(false);
  }

  // ── Section rail + scroll-spy ─────────────────────────────────────────────────────────────────
  // Computed at TOP level — hooks cannot live inside the detail branch below. Entries are
  // filtered by what this session can actually see, so a capability-hidden section never leaves
  // a dead link, and the spy observes exactly the filtered set. Order mirrors the page.
  const canPayments = caps.includes("cap.payments.manage"); // A7: the payments card (admin-only cap)
  const canArchiveViewer = caps.includes("cap.job.archive");
  const canSeeMaterials = caps.includes("cap.materials.receive") || caps.includes("cap.materials.manage");
  const railSections = selectedJob
    ? [
        { id: "jd-manage", label: "Manage", on: canAssignTasks },
        { id: "jd-client", label: "Client", on: Boolean(selectedJob.client) },
        { id: "jd-crew", label: "Crew", on: true },
        { id: "jd-tasks", label: "Tasks", on: true },
        { id: "jd-time", label: "Time", on: true },
        { id: "jd-equipment", label: "Equipment", on: true },
        { id: "jd-materials", label: "Materials", on: canSeeMaterials },
        { id: "jd-schedule", label: "Schedule", on: Boolean(onOpenSchedule) },
        { id: "jd-payments", label: "Payments", on: canPayments },
        { id: "jd-weekly", label: "Weekly report", on: Boolean(onOpenWeeklyReport) },
        { id: "jd-inspections", label: "Inspections", on: true },
        { id: "jd-daily", label: "Daily requirements", on: canChecklist },
        // The archive block itself is double-gated (cap + worker-withheld job.archive);
        // the rail entry follows the same pair so it can never point at a hidden section.
        { id: "jd-danger", label: "Danger zone", on: canArchiveViewer && selectedJob.archive != null },
      ].filter((s) => s.on)
    : [];

  // Scroll-spy via the shared hook (behavior-frozen extraction — same observer options).
  // observeKey: selectedJob keys re-observation after every reload (the sections remount);
  // the ids key inside the hook re-observes when the filtered set itself changes.
  const activeSection = useScrollSpy(railSections.map((s) => s.id), {
    enabled: view === "detail" && selectedJob !== null,
    observeKey: selectedJob,
  });

  if (view === "detail" && selectedJob) {
    const job = selectedJob;
    // R7 — assign-picker option state, mirroring the Worker's checkTaskTarget: an assign-only
    // manager may only target 'submitter'-linked personnel; a no-login person has NO role (NULL)
    // and is 403'd too. Ineligible options render DISABLED with a hint instead of a guaranteed 403.
    const assignOptionState = (p: api.DetailCrewMember): { disabled: boolean; hint: string } => {
      if (!isAssignOnlyActor || p.account_role === "submitter") return { disabled: false, hint: "" };
      return { disabled: true, hint: p.account_role ? ` (${p.account_role})` : " (no login)" };
    };
    // (W1 mirror) an assign-only manager may not touch a task currently HELD by a non-submitter —
    // the whole assign select locks for such a task (owner resolved through the crew leg; an
    // out-of-crew owner is unknown here, so it stays enabled and the Worker re-gates).
    const taskAssignLocked = (t: api.Task): boolean => {
      if (!isAssignOnlyActor || t.personnel_id == null) return false;
      const owner = job.crew.find((p) => p.id === t.personnel_id);
      return owner !== undefined && owner.account_role !== "submitter";
    };
    // R7 — status-control gating, mirroring checkTaskStatusOwnership: an own-only actor may only
    // move tasks assigned to their OWN linked personnel; managers/admins are unrestricted.
    const canTouchStatus = (t: api.Task): boolean =>
      canOwnTasks && (!isOwnOnlyActor || (viewerPersonnel !== null && t.personnel_id === viewerPersonnel.id));

    // ── Hero figures. Derived from the legs ALREADY loaded, so the hero costs no fetch.
    // Both counts are honest about being partial: tasks and time entries are paged (the
    // "Load more" legs), so these describe what is on screen, not the job's lifetime
    // totals. That is why nothing here is framed as a percentage or a total.
    // The voided→0 / null-hours→0 arithmetic lives (tested) in lib/jobtracker_view.
    const openTasks = openTaskCount(job.tasks);
    const hoursLogged = loggedHours(job.time_entries);
    return (
      <PageShell onHome={onBack} wide>
        <div className="dash-back-btn">
          <button onClick={handleBack} className="btn btn--secondary">← Back to jobs</button>
        </div>

        {/* Eyebrow + h1 — the house heading pair (Schedule/WPR precedent): surface name in the
            eyebrow, job identity in the h1. The eyebrow's gold rule is this page's one gold. */}
        <p className="page__heading--eyebrow">Job tracker</p>
        <div className="dash-detail__head">
          <h1 className="page__heading">{job.project_name}</h1>
          {/* Lifecycle, NOT status: `status` collapses inactive+archived into 'closed', so this
              pill read "Inactive" on an archived job. The class still keys off `status` — the
              pill COLOUR is a two-state open/closed signal and is unchanged. */}
          <span className={jobPillClass(job.status)}>{lifecycleLabel(job.lifecycle)}</span>
        </div>
        <p className="dash-card__sub muted">{(job.client?.name ?? "No client")} · {job.job_id}</p>

        {/* HERO — what this job IS, before nineteen sections of controls. Counts only:
            a job has no single completion number, and inventing one would be a figure
            nobody could reconcile. Open tasks reads DANGER when there are any, because
            that is the one number here that asks somebody to do something.
            Deliberately NOT .dash-progress — the detail view asserts that class is
            absent, and a meter would be exactly the fabricated number this avoids. */}
        <div className="job-hero">
          <ul className="job-hero__stats">
            <li className="job-hero__stat">
              <span className="job-hero__figure">{job.crew.length}</span>
              <span className="job-hero__label">On crew</span>
            </li>
            <li className="job-hero__stat">
              <span
                className={`job-hero__figure${openTasks > 0 ? " job-hero__figure--alert" : ""}`}
              >
                {openTasks}
              </span>
              <span className="job-hero__label">Open tasks</span>
            </li>
            <li className="job-hero__stat">
              <span className="job-hero__figure">{job.equipment_on_site.length}</span>
              <span className="job-hero__label">Equipment</span>
            </li>
            <li className="job-hero__stat">
              <span className="job-hero__figure">{fmtHours(hoursLogged)}</span>
              <span className="job-hero__label">Hours logged</span>
            </li>
          </ul>
        </div>

        {setupBanner === job.job_id && (
          <section className="wpr-banner wpr-banner--ok" aria-label="Finish setting up job">
            <span>
              <strong>Finish setting up {job.job_id}</strong> — assign crew, equipment, tasks, and the
              expected materials below to get this job ready.{" "}
              <button type="button" className="btn btn--secondary" onClick={() => setSetupBanner(null)}>Done</button>
            </span>
          </section>
        )}

        {actionMsg && (
          <p className={`wpr-banner ${actionMsg.ok ? "wpr-banner--ok" : "wpr-banner--warn"}`} role="status">
            {actionMsg.text}
          </p>
        )}
        {detailError && (
          <SectionError message={detailError.text} onRetry={detailError.retry} what="refreshing this job" />
        )}
        {pickerError && (
          <SectionError message={pickerError} onRetry={() => void reloadPickers()} what="loading picker options" />
        )}

        {/* SECTION RAIL + BODY — the weekly report's two-column shell: a horizontal chip
            scroller under the hero on a phone, a sticky vertical index beside one column
            of cards from 1024px. Entries come pre-filtered (railSections, top level), so a
            capability-hidden section never leaves a dead link; the scroll-spy lights the
            item for the section in view. */}
        <div className="job-shell">
          <SectionRail
            sections={railSections}
            activeId={activeSection ?? railSections[0]?.id ?? null}
            ariaLabel="Job sections"
            classPrefix="job-rail"
          />
          <div className="job-shell__body">

        {canAssignTasks && (
          <section className="card dash-section" id="jd-manage">
            <header className="job-sec__head">
              <h2 className="job-sec__title">Manage job</h2>
            </header>
            <form onSubmit={submitAddTask} className="dash-row" aria-label="Add a task">
              <input
                value={taskDesc}
                onChange={(e) => setTaskDesc(e.target.value)}
                placeholder="New task description"
                maxLength={256}
              />{" "}
              <label className="dash-card__label">
                Assign to:{" "}
                <select value={taskPerson} onChange={(e) => setTaskPerson(e.target.value)} aria-label="Assign new task to">
                  <option value="">— unassigned —</option>
                  {job.crew.map((p) => {
                    const s = assignOptionState(p);
                    return (
                      <option key={p.id} value={p.id} disabled={s.disabled}>
                        {p.name}
                        {s.hint}
                      </option>
                    );
                  })}
                </select>
              </label>{" "}
              {/* (G2.6) optional deadline — deliberately no max (future dates fine). */}
              <label className="dash-card__label">
                Due:{" "}
                <input
                  type="date"
                  aria-label="New task due date"
                  value={taskDue}
                  onChange={(e) => setTaskDue(e.target.value)}
                />
              </label>{" "}
              <button type="submit" disabled={actionBusy} className="btn btn--primary">Add task</button>
            </form>
            {/* Lifecycle + routing are job-lifecycle authority — admin-only (cap.jobtracker.manage).
                A manager (cap.tasks.assign) gets the add-task control above but NOT these. */}
            {canManage && (
              <>
                {/* "Archived" is NO LONGER an option here. Selecting it used to trigger a
                    four-sheet cross-workspace relocation on the next mirror cycle — unconfirmed,
                    un-retryable, and invisible afterwards because the pill re-derived from the
                    legacy status. The worker now refuses it too (409 use_archive_route), so a
                    stale bundle cannot fire it either. Archiving gets its own confirmed action
                    (ROADMAP Track 6).

                    On an already-archived job the whole control is locked: you must un-archive
                    first, because setting Active while the job's folders sit in the archive would
                    put the two systems out of step. */}
                <form className="dash-row" aria-label="Set job lifecycle">
                  <label className="dash-card__label">
                    Lifecycle:{" "}
                    <select
                      aria-label="Job lifecycle"
                      value={lifecycleSel}
                      disabled={actionBusy || job.lifecycle === "archived"}
                      onChange={(e) => submitLifecycle(e.target.value as api.JobLifecycle)}
                    >
                      <option value="active">Active</option>
                      <option value="inactive">Inactive</option>
                      {/* Rendered ONLY so a value={'archived'} select is not left with no matching
                          option (React would blank it). Disabled — never selectable. */}
                      {job.lifecycle === "archived" && (
                        <option value="archived" disabled>Archived</option>
                      )}
                    </select>
                  </label>
                  {job.lifecycle === "archived" && (
                    <span className="dash-card__sub muted">Un-archive this job to change its state.</span>
                  )}
                </form>
                <div className="dash-row">
                  {editContactsOpen ? (
                    <form onSubmit={submitEditContacts} aria-label="Edit routing and contacts">
                      <RoutingFields routing={editRouting} onChange={setEditRouting} />
                      <div className="dash-row">
                        <button type="submit" disabled={actionBusy} className="btn btn--primary">Save routing</button>{" "}
                        <button type="button" onClick={() => setEditContactsOpen(false)} className="btn btn--secondary">Cancel</button>
                      </div>
                    </form>
                  ) : (
                    <button
                      type="button"
                      onClick={() => {
                        setEditRouting(routingFormFromJob(job));
                        setEditContactsOpen(true);
                      }}
                      className="btn btn--edit"
                    >
                      Edit job details
                    </button>
                  )}
                </div>
              </>
            )}
          </section>
        )}

        {job.client && (
          <section className="card dash-section" id="jd-client">
            <header className="job-sec__head">
              <h2 className="job-sec__title">Client</h2>
            </header>
            <div>{job.client.name}</div>
            <div className="muted">
              {[job.client.contact, job.client.phone, job.client.email].filter(Boolean).join(" · ") || "—"}
            </div>
          </section>
        )}

        <section className="card dash-section" id="jd-crew">
          <header className="job-sec__head">
            <h2 className="job-sec__title">Assigned crew</h2>
            <span className="job-sec__count">{job.crew.length} on crew</span>
            {setupBanner === job.job_id && job.crew.length === 0 && (
              <span className="dash-pill dash-pill--warn">needs crew</span>
            )}
          </header>
          {job.crew.length ? (
            <div className="dash-chips">
              {job.crew.map((p) => (
                <span className="dash-chip" key={p.id}>
                  {p.name}{p.trade ? ` · ${p.trade}` : ""}
                  {canAssignCrew && (
                    <>
                      {" "}
                      <ChipX
                        ariaLabel={`Remove ${p.name} from crew`}
                        disabled={actionBusy}
                        onConfirm={() => removeCrew(p.id)}
                      />
                    </>
                  )}
                </span>
              ))}
            </div>
          ) : (
            <div className="sched-empty">
              <p className="sched-empty__title">No crew on this job</p>
              <p>
                {canAssignCrew
                  ? "Assign someone with the picker below."
                  : "People placed on this job will appear here."}
              </p>
            </div>
          )}
          {canAssignCrew && (
            <form onSubmit={submitAssignCrew} className="dash-row" aria-label="Assign crew to job">
              <select
                value={crewToAdd}
                onChange={(ev) => setCrewToAdd(ev.target.value)}
                aria-label="Crew member to place"
              >
                <option value="">— select crew member —</option>
                {crewOpts
                  .filter((p) => p.current_job !== job.job_id)
                  .map((p) => (
                    <option key={p.id} value={p.id}>{p.name}{p.trade ? ` · ${p.trade}` : ""}</option>
                  ))}
              </select>{" "}
              <button type="submit" disabled={actionBusy} className="btn btn--primary">Add to crew</button>
            </form>
          )}
        </section>

        <section className="card dash-section" id="jd-tasks">
          <header className="job-sec__head">
            <h2 className="job-sec__title">Tasks</h2>
            {/* "shown", never a total — the tasks leg is paged (see the hero comment). */}
            <span className="job-sec__count">{openTasks} open · {job.tasks.length} shown</span>
          </header>
          {job.tasks.length ? (
            <ul className="dash-tasklist">
              {job.tasks.map((t) => {
                const rowBusy = taskBusyIds.has(t.id);
                const assignLocked = taskAssignLocked(t);
                return (
                  <li key={t.id}>
                    <span className={taskPillClass(t.status)}>{statusLabel(t.status)}</span> {t.description}
                    {/* (G2.6) deadline + the shared Overdue pill (not-done AND due < Pacific-today). */}
                    <TaskDue dueDate={t.due_date} status={t.status} />
                    {t.personnel_name && !canAssignTasks ? <span className="muted"> — {t.personnel_name}</span> : null}
                    {canTouchStatus(t) && (
                      <>
                        {" "}
                        <select
                          aria-label={`Set status for task ${t.id}`}
                          value={t.status}
                          disabled={rowBusy}
                          onChange={(e) => changeTaskStatus(t.id, e.target.value as api.TaskStatus)}
                        >
                          <option value="open">{statusLabel("open")}</option>
                          <option value="in_progress">{statusLabel("in_progress")}</option>
                          <option value="done">{statusLabel("done")}</option>
                        </select>
                      </>
                    )}
                    {canAssignTasks && (
                      <>
                        {" "}
                        <select
                          aria-label={`Assign task ${t.id}`}
                          value={t.personnel_id != null ? String(t.personnel_id) : ""}
                          disabled={actionBusy || rowBusy || assignLocked}
                          title={
                            assignLocked
                              ? "Managers can only reassign tasks held by subcontractor accounts."
                              : undefined
                          }
                          onChange={(e) => submitReassignTask(t.id, e.target.value === "" ? null : Number(e.target.value))}
                        >
                          <option value="">— unassigned —</option>
                          {job.crew.map((p) => {
                            const s = assignOptionState(p);
                            return (
                              <option key={p.id} value={p.id} disabled={s.disabled}>
                                {p.name}
                                {s.hint}
                              </option>
                            );
                          })}
                          {t.personnel_id != null && !job.crew.some((p) => p.id === t.personnel_id) && (
                            <option value={t.personnel_id}>{t.personnel_name ?? `#${t.personnel_id}`}</option>
                          )}
                        </select>
                      </>
                    )}
                    {taskRowMsgs[t.id] ? <> <InlineRowMsg msg={taskRowMsgs[t.id]} /></> : null}
                  </li>
                );
              })}
            </ul>
          ) : (
            <div className="sched-empty">
              <p className="sched-empty__title">No tasks yet</p>
              <p>
                {canAssignTasks
                  ? "Add the first task with the form above."
                  : "Tasks assigned on this job will appear here."}
              </p>
            </div>
          )}
          {taskCursor && <LoadMoreBtn leg="task" />}
        </section>

        <section className="card dash-section" id="jd-time">
          <header className="job-sec__head">
            <h2 className="job-sec__title">Time entries</h2>
            <span className="job-sec__count">{fmtHours(hoursLogged)} h logged</span>
          </header>
          {/* R7 — a closed job (legacy status 'closed' ⟺ jobs.active=0, which the Worker's
              time-write job guard rejects) says so instead of silently dropping the form.
              G2.3 — EXCEPT in amend mode: corrections are deliberately allowed on a closed job
              (the amend inherits its job binding server-side; blocking would recreate the
              "a wrong entry can't be corrected by anyone" problem this epic exists to fix). */}
          {canLogTime && job.status === "closed" && !amendTarget && (
            <div className="dash-unavail">This job is closed — time can't be logged. Existing entries can still be corrected.</div>
          )}
          {canLogTime &&
            (job.status !== "closed" || amendTarget) && (
              <>
                {isSubcontractor && myCrewError && (
                  <SectionError message={myCrewError} onRetry={loadMyCrew} what="loading your crew" />
                )}
                {amendTarget && (
                  <div className="wpr-banner wpr-banner--ok" role="status">
                    Correcting the {fmtHours(amendTarget.hours)}h entry for {amendTarget.personnel_name ?? "Job-level"} recorded{" "}
                    {fmtDateTime(amendTarget.recorded_at)} — saving creates a corrected copy; the original stays in the record.{" "}
                    <button type="button" className="btn btn--secondary" onClick={resetTimeForm}>
                      Cancel
                    </button>
                  </div>
                )}
                <form onSubmit={submitLogTime} className="dash-row" aria-label={amendTarget ? "Correct time entry" : "Log time"}>
                  <input
                    value={logHours}
                    onChange={(e) => {
                      setLogHours(e.target.value);
                      if (hoursError) setHoursError(null);
                    }}
                    placeholder="Hours"
                    inputMode="decimal"
                    size={5}
                    aria-invalid={hoursError ? true : undefined}
                  />{" "}
                  {hoursError && (
                    <span className="dash-pill dash-pill--danger" role="alert">
                      {hoursError}
                    </span>
                  )}{" "}
                  <label className="dash-card__label">
                    For:{" "}
                    {/* R7 — explicit attribution: "Me (<name>)" resolves the viewer's OWN linked
                        personnel id (the default when linked); "Job-level (no person)" is the
                        deliberate no-subject choice. The old "— me / unassigned —" default logged
                        payroll-grade time attributed to nobody. Subcontractor options annotate
                        people currently placed on OTHER jobs (the Worker's 403 stays the gate). */}
                    <select value={logPerson} onChange={(e) => setLogPerson(e.target.value)} aria-label="Log time for">
                      {viewerPersonnel && (
                        <option value={String(viewerPersonnel.id)}>Me ({viewerPersonnel.name})</option>
                      )}
                      <option value="">Job-level (no person)</option>
                      {(isSubcontractor ? myCrew : job.crew)
                        .filter((p) => !viewerPersonnel || p.id !== viewerPersonnel.id)
                        .map((p) => {
                          const placement = isSubcontractor
                            ? (p as MyCrewMember).current_job === job.job_id
                              ? ""
                              : (p as MyCrewMember).current_job
                                ? ` — on ${(p as MyCrewMember).current_job}`
                                : " — unplaced"
                            : "";
                          return (
                            <option key={p.id} value={p.id}>
                              {p.name}
                              {placement}
                            </option>
                          );
                        })}
                    </select>
                  </label>{" "}
                  {!viewerPersonnel && (
                    <span className="muted">
                      Your account isn't linked to a roster person — this logs job-level unless you pick someone.
                    </span>
                  )}{" "}
                  <label className="dash-card__label">
                    Task:{" "}
                    <select value={logTask} onChange={(e) => setLogTask(e.target.value)} aria-label="Log time task">
                      <option value="">— job-level —</option>
                      {job.tasks.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.description}
                          {t.status === "done" ? " (done)" : ""}
                        </option>
                      ))}
                    </select>
                  </label>{" "}
                  <input
                    value={logNotes}
                    onChange={(e) => setLogNotes(e.target.value)}
                    placeholder="Notes (optional)"
                    maxLength={2000}
                  />{" "}
                  <button type="submit" disabled={actionBusy} className="btn btn--primary">
                    {amendTarget ? "Save correction" : "Log time"}
                  </button>
                </form>
              </>
            )}
          {job.time_entries.length ? (
            <table className="dash-table dash-table--stack">
              <thead>
                <tr>
                  <th>Who</th>
                  <th>Hours</th>
                  <th>Task</th>
                  <th>By</th>
                  <th>Recorded</th>
                  <th>Notes</th>
                  {job.time_entries.some((t) => t.can_amend) && <th>Fix</th>}
                </tr>
              </thead>
              <tbody>
                {/* G2.3 — the list shows chain HEADS only (worker-filtered). A head that IS a
                    correction gets a "corrected" pill; a VOID (corrected to 0h) renders struck
                    through. Edit/Void render only when the Worker would accept (can_amend =
                    recorder-or-manager, worker-computed) — Edit stays on a voided row (amending a
                    void is the recovery path); Void hides there (a void of a void is meaningless). */}
                {job.time_entries.map((t) => (
                  <tr key={t.uuid} className={t.voided ? "dash-row job-time--voided" : "dash-row"}>
                    <td className="dash-table__name">
                      {t.personnel_name ?? "Job-level"}
                      {t.voided ? (
                        <>
                          {" "}
                          <span className="dash-pill dash-pill--danger">voided</span>
                        </>
                      ) : t.amended ? (
                        <>
                          {" "}
                          <span className="dash-pill" title="This entry is a correction of an earlier one.">corrected</span>
                        </>
                      ) : null}
                    </td>
                    <td data-cell="Hours">{fmtHours(t.hours)}</td>
                    <td data-cell="Task">{t.task_description ?? "—"}</td>
                    <td data-cell="By">{t.recorded_by_name ?? "—"}</td>
                    <td data-cell="Recorded">{fmtDateTime(t.recorded_at)}</td>
                    <td data-cell="Notes">{t.notes ?? ""}</td>
                    {job.time_entries.some((x) => x.can_amend) && (
                      <td data-cell="Fix">
                        {t.can_amend && (
                          <>
                            <button
                              type="button"
                              className="btn btn--secondary"
                              disabled={timeRowBusy !== null || actionBusy}
                              aria-label={`Edit time entry ${t.uuid}`}
                              onClick={() => startAmend(t)}
                            >
                              Edit
                            </button>
                            {!t.voided && (
                              <>
                                {" "}
                                <button
                                  type="button"
                                  className="btn btn--secondary"
                                  disabled={timeRowBusy !== null || actionBusy}
                                  aria-label={`Void time entry ${t.uuid}`}
                                  onClick={() => {
                                    setVoidUuid(voidUuid === t.uuid ? null : t.uuid);
                                    setVoidReason("");
                                    setTimeRowMsg(t.uuid, null);
                                  }}
                                >
                                  Void
                                </button>
                              </>
                            )}
                            {voidUuid === t.uuid && !t.voided && (
                              <>
                                {" "}
                                <input
                                  value={voidReason}
                                  onChange={(e) => setVoidReason(e.target.value)}
                                  placeholder="Reason (required)"
                                  maxLength={2000}
                                  aria-label={`Void reason for ${t.uuid}`}
                                />{" "}
                                <button
                                  type="button"
                                  className="btn btn--danger"
                                  disabled={timeRowBusy !== null}
                                  aria-label={`Confirm void ${t.uuid}`}
                                  onClick={() => void submitVoid(t)}
                                >
                                  {timeRowBusy === t.uuid ? "Voiding…" : "Confirm void"}
                                </button>
                              </>
                            )}
                            {timeRowMsgs[t.uuid] && (
                              <>
                                {" "}
                                <InlineRowMsg msg={timeRowMsgs[t.uuid]} />
                              </>
                            )}
                          </>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="sched-empty">
              <p className="sched-empty__title">No time logged</p>
              <p>
                {canLogTime && job.status !== "closed"
                  ? "Log the first hours with the form above."
                  : "Hours logged against this job will appear here."}
              </p>
            </div>
          )}
          {timeCursor && <LoadMoreBtn leg="time" />}
        </section>

        <section className="card dash-section" id="jd-equipment">
          <header className="job-sec__head">
            <h2 className="job-sec__title">Equipment on site</h2>
            <span className="job-sec__count">{job.equipment_on_site.length} on site</span>
            {setupBanner === job.job_id && job.equipment_on_site.length === 0 && (
              <span className="dash-pill dash-pill--warn">needs equipment</span>
            )}
          </header>
          {job.equipment_on_site.length ? (
            <div className="dash-chips">
              {job.equipment_on_site.map((e) => (
                <span className="dash-chip" key={e.id}>{e.name}{e.identifier ? ` · ${e.identifier}` : ""}</span>
              ))}
            </div>
          ) : (
            <div className="sched-empty">
              <p className="sched-empty__title">No equipment on site</p>
              <p>
                {canFieldEquip
                  ? "Move a unit here with the picker below."
                  : "Equipment moved to this job will appear here."}
              </p>
            </div>
          )}
          {canFieldEquip && (
            <form onSubmit={submitAssignEquip} className="dash-row" aria-label="Assign equipment to job">
              <select
                value={equipToAdd}
                onChange={(ev) => setEquipToAdd(ev.target.value)}
                aria-label="Equipment to move here"
              >
                <option value="">— select equipment —</option>
                {equipOpts.map((eq) => (
                  <option key={eq.id} value={eq.id}>{eq.name}{eq.identifier ? ` · ${eq.identifier}` : ""}</option>
                ))}
              </select>{" "}
              <button type="submit" disabled={actionBusy} className="btn btn--primary">Move here</button>
            </form>
          )}
        </section>

        {/* Material receipts M1 — self-contained (own caps/fetch/state; the D4 parallel-build rule). */}
        {/* Gated on the SAME two caps ExpectedMaterialsSection checks internally: the
            component returns null without them, but an ungated drawer would still render
            its shell — an empty "Materials" disclosure advertising a capability the
            session does not have.
            CLOSED by default. This is the same component the Materials page renders, and
            on a job with a BOM it put every expected line straight onto a page that already
            carries nineteen sections. The drawer means materials are one tap away instead
            of always in the way — the posture the Schedule page's copy already uses. */}
        {canSeeMaterials && (
        <details className="sched-drawer" id="jd-materials" aria-label="Expected materials">
          <summary>
            Materials
            <span className="sched-drawer__note">Expected lines, loads and delivery marks</span>
          </summary>
          <div className="sched-drawer__body">
          <ExpectedMaterialsSection
            jobId={job.job_id}
            onOpenMaterials={onOpenMaterials ? () => onOpenMaterials(job.job_id) : undefined}
          />
          </div>
        </details>
        )}

        {/* ADR-0006 PR-4 — the per-job Schedule deep-link card, beside Materials. All roles
            see it (schedule visibility is all-roles, decision 4); the import affordances on
            the page itself re-gate on cap.jobtracker.manage server-side. */}
        {onOpenSchedule && (
          <section className="card dash-section job-link" id="jd-schedule">
            <header className="job-sec__head">
              <h2 className="job-sec__title">Schedule</h2>
            </header>
            <p className="dash-hint">
              The job&apos;s living task list — what the project schedule says is happening,
              with dates, milestones and deliveries.
            </p>
            <button className="btn btn--secondary" onClick={() => onOpenSchedule(job.job_id)}>
              Open schedule →
            </button>
          </section>
        )}

        {canPayments && (
          <JobPaymentsCard
            jobId={job.job_id}
            onOpenSchedule={onOpenSchedule ? () => onOpenSchedule(job.job_id) : undefined}
          />
        )}

        {/* 0067 — the office's weekly-report inputs (the sections D1 cannot derive). Deep-link
            card beside Materials; rendered only for the office capability, and the Worker
            re-gates every call regardless of what this hides. */}
        {onOpenWeeklyReport && (
          <section className="card dash-section job-link" id="jd-weekly">
            <header className="job-sec__head">
              <h2 className="job-sec__title">Weekly production report</h2>
            </header>
            <p className="dash-hint">
              Safety statistics, labor hours by company, pending RFIs and submittals, weather days
              and photo selection for the client-facing weekly report.
            </p>
            <button className="btn btn--secondary" onClick={() => onOpenWeeklyReport(job.job_id)}>
              Open weekly report inputs →
            </button>
          </section>
        )}

        <section className="card dash-section" id="jd-inspections">
          <header className="job-sec__head">
            <h2 className="job-sec__title">Inspections</h2>
            <span className="job-sec__count">{job.inspections.length} shown</span>
          </header>
          {job.inspections.length ? (
            /* --stack + data-cell: this was the one table on the page without the
               kit's phone treatment, so three columns scrolled sideways instead of
               becoming labelled blocks. */
            <table className="dash-table dash-table--stack">
              <thead>
                <tr>
                  <th>Form</th>
                  <th>Equipment</th>
                  <th>Performed</th>
                </tr>
              </thead>
              <tbody>
                {job.inspections.map((i) => (
                  <tr key={i.uuid} className="dash-row">
                    <td>{i.form_code} v{i.version}</td>
                    <td data-cell="Equipment">{i.equipment_name ?? "—"}</td>
                    <td data-cell="Performed">{fmtDateTime(i.performed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="sched-empty">
              <p className="sched-empty__title">No inspections yet</p>
              <p>Inspections performed on this job&apos;s equipment will appear here.</p>
            </div>
          )}
          {inspCursor && <LoadMoreBtn leg="insp" />}
        </section>

        {/* D4 — per-job daily-form requirements (self-contained section; cap.checklist.manage).
            The wrapper div only carries the rail anchor — the component owns its section. */}
        {canChecklist && (
          <div id="jd-daily">
            <JobDailyRequirementsSection jobId={job.job_id} />
          </div>
        )}

        {/* DANGER ZONE — last, not ninth of nineteen. Archiving relocates seven
            containers across Smartsheet and Box; it used to render between the routing
            form and the client card, in the middle of the reading order. A destructive
            action belongs at the end, behind its own heading. The WRAPPER is gated on the
            same pair the panel checks (cap.job.archive + the worker-withheld job.archive):
            the panel alone self-hiding left every other viewer a bare red "Danger zone"
            label over nothing. */}
        {canArchiveViewer && job.archive && (
          <div className="job-danger" id="jd-danger">
            <p className="job-danger__label">Danger zone</p>
            <JobArchivePanel
              job={job}
              canArchive={canArchiveViewer}
              onChanged={reloadDetail}
            />
          </div>
        )}
          </div>
        </div>
      </PageShell>
    );
  }

  // A PENDING detail open (deep link, list click, post-create route-in) renders the detail
  // chrome with skeletons. Without this branch the component fell through and put the whole
  // JOB LIST on screen for the load window of every cold /jobs/:id visit. The failure path
  // already resets view to "list" (openJobById), so this branch cannot dead-end.
  if (view === "detail") {
    return (
      <PageShell onHome={onBack} wide>
        <div className="dash-back-btn">
          <button onClick={handleBack} className="btn btn--secondary">← Back to jobs</button>
        </div>
        <p className="page__heading--eyebrow">Job tracker</p>
        <div aria-busy="true" aria-label="Loading job">
          <div className="sched-skel" />
          <div className="sched-skel" />
          <div className="sched-skel" />
        </div>
      </PageShell>
    );
  }

  // List view
  return (
    <PageShell onHome={onBack}>

      <h2 className="page__heading">Job Tracker</h2>
      <div className="dash-row">
        <label className="dash-card__label">Status:{" "}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as api.JobStatusFilter)}
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
      </div>

      {canManage && (
        <div className="dash-row">
          {newJobOpen ? (
            <form onSubmit={submitNewJob} aria-label="Create job">
              <p className="dash-card__sub muted">A Job ID (JOB-######) is assigned automatically on create.</p>
              <div className="dash-row">
                <input
                  value={newJobName}
                  onChange={(e) => setNewJobName(e.target.value)}
                  placeholder="Project name"
                  maxLength={256}
                />{" "}
                <select
                  aria-label="Client"
                  value={newJobClientId}
                  onChange={(e) => {
                    setNewJobClientId(e.target.value);
                    if (e.target.value !== "new") setNewJobClient("");
                  }}
                >
                  <option value="">No client</option>
                  {api.collapseClients(clientOptions).map((cl) => (
                    <option key={cl.id} value={String(cl.id)}>{cl.name}</option>
                  ))}
                  <option value="new">+ New client…</option>
                </select>
                {newJobClientId === "new" && (
                  <>
                    {" "}
                    <input
                      aria-label="New client name"
                      value={newJobClient}
                      onChange={(e) => setNewJobClient(e.target.value)}
                      placeholder="New client name"
                      maxLength={256}
                    />
                  </>
                )}
              </div>
              <RoutingFields routing={createRouting} onChange={setCreateRouting} showName={false} safetyCcContacts={deliveryContacts} />
              <div className="dash-row">
                <button type="submit" disabled={actionBusy} className="btn btn--primary">Create</button>{" "}
                <button
                  type="button"
                  onClick={() => {
                    setNewJobOpen(false);
                    setCreateRouting(EMPTY_ROUTING);
                    // Clear the client selection too — a cancelled form that reopens still
                    // holding the last pick would silently attach the NEXT job to it.
                    setNewJobClientId("");
                    setNewJobClient("");
                  }}
                  className="btn btn--secondary"
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <button onClick={() => setNewJobOpen(true)} className="btn btn--primary">+ New job</button>
          )}
        </div>
      )}
      {actionMsg && (
        <p className={`wpr-banner ${actionMsg.ok ? "wpr-banner--ok" : "wpr-banner--warn"}`} role="status">
          {actionMsg.text}
        </p>
      )}
      {/* R7 never-silent: list failures carry a working Retry, and the error is mutually exclusive
          with the empty state (an error must never masquerade as "no jobs"). */}
      {listError && <SectionError message={listError.text} onRetry={listError.retry} what="loading jobs" />}

      {jobs.length === 0 ? (
        loading ? (
          <div className="muted">Loading jobs…</div>
        ) : listError ? null : (
          <div className="dash-unavail">No jobs for this status.</div>
        )
      ) : (
        <>
          <div className="dash-grid">
            {jobs.map((job) => (
              <div
                key={job.job_id}
                onClick={() => handleCardClick(job)}
                className="dash-card--click"
                role="button"
              >
                <div className="dash-card__head">
                  <h3 className="dash-card__title">{job.project_name}</h3>
                  {/* R7 — "Your job": the viewer's own placement, their direct path to log time. */}
                  {viewerCurrentJob === job.job_id && <span className="dash-pill dash-pill--ok">Your job</span>}
                  {/* Lifecycle, not status — an archived job must be legible from the list without
                      opening it. Pill colour still keys off `status` (open vs closed). */}
                  <span className={jobPillClass(job.status)}>{lifecycleLabel(job.lifecycle)}</span>
                </div>
                <div className="dash-card__sub">{(job.client_name ?? "No client")} · {job.job_id}</div>

                {job.crew.length > 0 && (
                  <div className="dash-chips">
                    {job.crew.map((p) => (
                      <span className="dash-chip" key={p.id}>{p.name}</span>
                    ))}
                  </div>
                )}

                {job.open_tasks.length > 0 && (
                  <ul className="dash-tasklist">
                    {job.open_tasks.map((t) => (
                      <li key={t.id}>
                        <span className={taskPillClass(t.status)}>{statusLabel(t.status)}</span> {t.description}
                        {/* (G2.6) the card preview carries the same due/Overdue signal. */}
                        <TaskDue dueDate={t.due_date} status={t.status} />
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>

          {cursor && (
            <div className="dash-row dash-load-more">
              <button onClick={loadMore} disabled={loading} className="btn btn--secondary">
                {loading ? "Loading..." : "Load more"}
              </button>
            </div>
          )}
        </>
      )}
    </PageShell>
  );
}
