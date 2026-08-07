// R1 — shared human labels for the field-ops wire vocabulary. Raw internal tokens (snake_case
// statuses, item_type keys, template origins) must never reach the UI as copy — pages render THESE.
// The KEYS are unchanged everywhere (API payloads, option values, branching) — label only, the same
// rule as roleLabel (src/lib/api.ts, the Slice-T display rename).
//
// Unknown tokens fall back to a humanized form (underscores → spaces, sentence case) so a new wire
// value degrades readably instead of leaking snake_case.

/** 'some_new_token' → 'Some new token'. */
function humanizeToken(token: string): string {
  const spaced = token.replace(/_/g, " ").trim();
  return spaced.length === 0 ? token : spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

const STATUS_LABELS: Record<string, string> = {
  open: "Open",
  in_progress: "In progress",
  done: "Done",
  complete: "Complete",
};

/** Task / checklist-item / instance status → pill copy (open/in_progress/done/complete). */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? humanizeToken(status);
}

const LIFECYCLE_LABELS: Record<string, string> = {
  active: "Active",
  inactive: "Inactive",
  archived: "Archived",
};

/** Job lifecycle → pill / selector copy (active | inactive | archived).
 *
 *  Deliberately separate from `statusLabel`: a JOB's state is `jobs.lifecycle`, never the legacy
 *  `status` column — that one collapses inactive AND archived into 'closed'. Rendering a job as
 *  statusLabel(job.status) is precisely what made every archived job read "Inactive". */
export function lifecycleLabel(lifecycle: string): string {
  return LIFECYCLE_LABELS[lifecycle] ?? humanizeToken(lifecycle);
}

const ITEM_TYPE_LABELS: Record<string, string> = {
  manual_attest: "Check",
  count: "Count",
  form_linked: "Form",
  inspection: "Inspection",
};

/** Checklist item_type → subtitle copy (manual_attest/count/form_linked/inspection). */
export function itemTypeLabel(itemType: string): string {
  return ITEM_TYPE_LABELS[itemType] ?? humanizeToken(itemType);
}

const ORIGIN_LABELS: Record<string, string> = {
  default: "Shared",
  override: "This job only",
};

/** Effective-checklist item origin → editor pill copy (default/override). */
export function originLabel(origin: string): string {
  return ORIGIN_LABELS[origin] ?? humanizeToken(origin);
}
