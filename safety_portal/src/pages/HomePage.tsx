import { useAuth } from "../lib/auth";
import { AppHeader } from "../components/AppHeader";

/** Home navigation targets — the views a card can open. Kept in sync with App's view switch. */
export type HomeNav =
  | "fill"
  | "request"
  | "accounts"
  | "forms"
  | "fieldops-jobs"
  | "fieldops-tasks"
  | "fieldops-site-tasks"
  | "fieldops-inspections"
  | "fieldops-equipment"
  | "fieldops-personnel"
  | "materials-catalog"
  | "po-builder"
  | "po-vendors"
  | "po-config"
  | "subcontractors"
  | "subcontract-builder";

/** R7 — Home is grouped into headed sections (it had grown into a 10-card flat wall under a single
 *  "Daily forms" heading, A4; the office-facing management cards split into their own "Office
 *  operations" section 2026-07). Section membership is presentation only: every card keeps its
 *  exact capability gate and view key. */
type HomeSectionKey = "forms" | "field" | "office" | "admin";

interface HomeCard {
  key: HomeNav;
  /** Capability required to see this card (migration 0013). null = everyone. */
  cap: string | null;
  badge: string;
  title: string;
  desc: string;
  section: HomeSectionKey;
}

const HOME_SECTIONS: { key: HomeSectionKey; heading: string }[] = [
  { key: "forms", heading: "Daily forms" },
  { key: "field", heading: "Field operations" },
  { key: "office", heading: "Office operations" },
  { key: "admin", heading: "Administration" },
];

// Array order = display order within each section. The form actions (Submit / Form Request) are
// IDENTICAL for every account, so an admin's home leads with the same cards as a field PM's;
// field-operations cards follow, and the management cards (capability-gated) close the page.
const HOME_CARDS: HomeCard[] = [
  {
    key: "fill",
    cap: "cap.form.submit",
    badge: "New",
    title: "Submit a form",
    desc: "Pick a job and form — safety or progress (JHA, Toolbox Talk, Equipment Pre-Inspection, Daily Report, and more).",
    section: "forms",
  },
  {
    key: "request",
    cap: "cap.form.request",
    badge: "Browse",
    title: "Form Request",
    desc: "Find a job's filed forms and download them on the spot — last week's JHAs, a crane lift plan, and more.",
    section: "forms",
  },
  {
    key: "fieldops-tasks",
    cap: "cap.tasks.own",
    badge: "Field Ops",
    // R7 (R2 finding) + D2: the card copy names the Daily report — the tab lives here too.
    title: "My Tasks",
    desc: "Your assigned tasks and inspections, plus your Daily report — grouped by job, updated as you work.",
    section: "field",
  },
  {
    key: "fieldops-site-tasks",
    cap: "cap.jobtracker.read",
    badge: "Field Ops",
    title: "Site Tasks",
    // Distinct from My Tasks (which is YOUR assignments across jobs): this is one JOB's whole
    // plate — the schedule's tasks with mark-off, plus every assigned one-off task.
    desc: "Pick a job and see its whole task list — the schedule's tasks with progress mark-off, plus assigned one-off tasks.",
    section: "field",
  },
  {
    key: "fieldops-jobs",
    cap: "cap.jobtracker.read",
    badge: "Field Ops",
    title: "Job Tracker",
    desc: "Jobs, crew, open tasks, and equipment on site.",
    section: "field",
  },
  {
    key: "fieldops-equipment",
    cap: "cap.equipment.field",
    badge: "Field Ops",
    title: "Equipment",
    desc: "Fleet readiness, current location, inspections, and machine logs.",
    section: "field",
  },
  {
    key: "fieldops-personnel",
    cap: "cap.personnel.read",
    badge: "Admin",
    title: "Personnel",
    desc: "Who is where, and per-person hour history.",
    section: "field",
  },
  // ── Office operations (2026-07): the office-facing management cards — POs, subcontracts, and the
  //    catalogs/directories/checklists behind them. Array order here IS the two-wide display order.
  //    Every card keeps its exact capability gate + view key; this is a presentation regrouping only.
  {
    key: "po-builder",
    cap: "cap.po.manage",
    badge: "Admin",
    // 2026-07 fold: RFQs + Vendor Estimates are TABS inside the Purchase Orders hub now
    // (one card, one view entry) — their old standalone cards are gone, gate unchanged.
    title: "Purchase Orders",
    desc: "Build vendor POs — line items, tax, and terms — request quotes with price-free RFQs, and import vendor estimates into editable draft POs, all tracked from draft to sent.",
    section: "office",
  },
  {
    key: "subcontract-builder",
    cap: "cap.subcontracts.manage",
    badge: "Admin",
    title: "Subcontracts",
    desc: "Build a subcontract package — scope, schedule of values, and terms — then track it from draft to executed.",
    section: "office",
  },
  {
    key: "fieldops-inspections",
    cap: "cap.checklist.manage",
    badge: "Admin",
    // R7 (Open Q4) → D2: the card is inspections-only now (the daily content lives in the
    // Daily Field Report form definition; the default-checklist editor was retired). Key unchanged.
    title: "Checklists",
    desc: "Author reusable inspection checklists and assign them to a manager or subcontractor.",
    section: "office",
  },
  {
    key: "materials-catalog",
    cap: "cap.materials.manage",
    badge: "Admin",
    title: "Materials Catalog",
    // Office operations (2026-07): the datasheet-backed material TYPE catalog also feeds the
    // purchase-order line-item picker, so it sits beside the PO cards. Gate unchanged
    // (cap.materials.manage — admin only); the page's list read still needs cap.materials.receive,
    // which admin also holds (migration 0013 catch-all), so access neither breaks nor widens.
    desc: "The datasheet-backed material type catalog behind purchase-order line items — add, edit, and retire types.",
    section: "office",
  },
  {
    key: "po-vendors",
    cap: "cap.po.manage",
    badge: "Admin",
    title: "Vendors",
    desc: "The vendor directory behind purchase orders — contacts, regions, supply categories, and terms.",
    section: "office",
  },
  {
    key: "subcontractors",
    cap: "cap.subcontracts.manage",
    badge: "Admin",
    title: "Subcontractors",
    desc: "The subcontractor directory behind subcontracts — contacts, trades, state, licenses, and terms.",
    section: "office",
  },
  // ── Administration: the config/identity + account cards. PO/SC Configuration leads (it edits both
  //    the PO and subcontract config), then the form catalog, then portal accounts.
  {
    key: "po-config",
    cap: "cap.po.manage",
    badge: "Admin",
    title: "PO/SC Configuration",
    desc: "Edit the purchaser identity, ship-to tax table, and terms versions that print on every PO — each change is queued for the operator's review.",
    section: "admin",
  },
  {
    key: "forms",
    cap: "cap.admin.formbuilder",
    badge: "Admin",
    title: "Forms",
    desc: "Manage the form catalog and publish new versions.",
    section: "admin",
  },
  {
    key: "accounts",
    cap: "cap.admin.accounts",
    badge: "Admin",
    title: "Accounts",
    desc: "Create, edit, disable, and set roles/capabilities on portal accounts.",
    section: "admin",
  },
];

/**
 * The unified home (P1). Every account lands here; the action cards are capability-gated
 * (migration 0013), so an admin sees the same form cards as a field PM PLUS their
 * management cards. Admin submit-as is preserved downstream: opening "Submit a form" routes
 * to FormFillPage, which still shows the "filled out as" account selector for admins.
 *
 * R7 — cards render under three headed sections (Daily forms / Field operations /
 * Administration); a section with no visible cards renders nothing, so a submitter never
 * sees an empty "Administration" heading. Gating and view keys are untouched.
 */
export function HomePage({ onNavigate }: { onNavigate: (v: HomeNav) => void }) {
  const { user, logout } = useAuth();
  const caps = user?.capabilities ?? [];
  const cards = HOME_CARDS.filter((c) => c.cap === null || caps.includes(c.cap));
  return (
    <div className="page">
      <AppHeader
        action={
          <button className="btn btn--ghost" onClick={() => void logout()}>
            Sign out
          </button>
        }
      />
      <main className="page__main">
        <p className="welcome">
          Signed in as <strong>{user?.username}</strong>
        </p>
        {HOME_SECTIONS.map((s) => {
          const visible = cards.filter((c) => c.section === s.key);
          if (visible.length === 0) return null;
          return (
            <section key={s.key} aria-label={s.heading}>
              {/* --eyebrow is ADDITIVE (design refinement 2026-07): the section header
                  renders as a letterspaced signage eyebrow; .page__heading is unrenamed. */}
              <h2 className="page__heading page__heading--eyebrow">{s.heading}</h2>
              <div className="form-grid">
                {visible.map((c) => (
                  <button key={c.key} className="form-card" onClick={() => onNavigate(c.key)}>
                    <span className="form-card__badge">{c.badge}</span>
                    <span className="form-card__title">{c.title}</span>
                    <span className="form-card__desc">{c.desc}</span>
                  </button>
                ))}
              </div>
            </section>
          );
        })}
      </main>
    </div>
  );
}
