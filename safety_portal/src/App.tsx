import { Component, Suspense, lazy, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useAuth } from "./lib/auth";
import { LoginPage } from "./pages/LoginPage";
import { HomePage, type HomeNav } from "./pages/HomePage";
import { FormFillPage, type FormPrefill } from "./pages/FormFillPage";
import { FormRequestPage } from "./pages/FormRequestPage";
import { FieldOpsJobTracker } from "./pages/FieldOpsJobTracker";
// EAGER on purpose: a manager marking a delivery is a field path, and the lazy split is reserved
// for office-desk admin surfaces (a chunk-load failure mid-shift must not take the daily path down).
import { JobMaterialsPage } from "./pages/JobMaterialsPage";
// EAGER for the same reason: the schedule is an all-roles field view (ADR-0006 decision 4).
import { JobSchedulePage } from "./pages/JobSchedulePage";
import { WeeklyReportPage } from "./pages/WeeklyReportPage";
import { FieldOpsMyTasks } from "./pages/FieldOpsMyTasks";
import { SiteTasksPage } from "./pages/SiteTasksPage";
import { FieldOpsInspections } from "./pages/FieldOpsInspections";
import { FieldOpsEquipment } from "./pages/FieldOpsEquipment";
import { FieldOpsPersonnel } from "./pages/FieldOpsPersonnel";
import { BackHomeNav } from "./components/BackHomeNav";
import type { PoTab } from "./pages/PurchaseOrdersPage";
import { useIdleLogout } from "./lib/useIdleLogout";
import {
  HOME_ROUTE,
  VIEW_CAPS,
  formatRoute,
  parseRoute,
  type AppRoute,
  type RoutePrefill,
} from "./lib/router";

// ── Admin-only route chunks (optimization #8) ────────────────────────────────────────────────────
// Exactly the ADMIN-ONLY views are code-split: Forms (which pulls the whole FormEditor /
// editorValidation / PublishMonitor stack into its chunk), Accounts, Materials Catalog, the
// three PO pages (builder + vendors + the read-only config view), and the two subcontract pages
// (subcontractors directory + subcontract builder) — office-desk surfaces a field phone never opens.
// Every FIELD-CRITICAL view (FormFillPage, My Tasks / DailyReportTab, Job Tracker, Equipment,
// Personnel, Inspections, Form Request) stays EAGER on purpose: a chunk-load failure mid-shift on
// a flaky cell connection must never take down the daily path. The Suspense fallback below is the
// design system's standard loading line (the same one the auth gate shows).
const AccountsPage = lazy(() =>
  import("./pages/AccountsPage").then((m) => ({ default: m.AccountsPage })),
);
const FormsPage = lazy(() => import("./pages/FormsPage").then((m) => ({ default: m.FormsPage })));
const MaterialsCatalogPage = lazy(() =>
  import("./pages/MaterialsCatalogPage").then((m) => ({ default: m.MaterialsCatalogPage })),
);
// Purchase-Orders hub (2026-07 fold): ONE lazy chunk carrying the three tab panels — the PO
// builder/tracker, the RFQ composer (ADR-0004 R1), and the vendor-estimate importer +
// disposition screen (ADR-0004 E1/E3) — office-desk surfaces, code-split together.
const PurchaseOrdersPage = lazy(() =>
  import("./pages/PurchaseOrdersPage").then((m) => ({ default: m.PurchaseOrdersPage })),
);
const PoVendorsPage = lazy(() =>
  import("./pages/PoVendorsPage").then((m) => ({ default: m.PoVendorsPage })),
);
const PoConfigPage = lazy(() =>
  import("./pages/PoConfigPage").then((m) => ({ default: m.PoConfigPage })),
);
const SubcontractorsPage = lazy(() =>
  import("./pages/SubcontractorsPage").then((m) => ({ default: m.SubcontractorsPage })),
);
const SubcontractBuilderPage = lazy(() =>
  import("./pages/SubcontractBuilderPage").then((m) => ({ default: m.SubcontractBuilderPage })),
);

// Never-silent for the lazy admin chunks (ops review): a chunk-load failure (offline / deploy skew)
// must show a visible error + Retry, not a white screen. Class component — the only React error-
// boundary mechanism. Retry re-attempts the dynamic import by remounting the Suspense subtree.
class ChunkBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed) {
      return (
        <div className="centered">
          <p className="banner banner--err">
            Couldn't load this page — check your connection.{" "}
            <button type="button" className="btn btn--secondary" onClick={() => this.setState({ failed: false })}>
              Retry
            </button>
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}

const DISCARD_PROMPT = "Discard this form? Your entries haven't been submitted.";

// Purchase-Orders hub: the routed view ↔ tab mapping (three routes, one component — the
// shared branch below keeps the hub instance alive across tab navigation). Tab flips use
// REPLACE semantics (the My Tasks precedent: a flip is within-page state, not a deeper step).
const PO_TAB_BY_VIEW = {
  "po-builder": "orders",
  "po-rfqs": "rfqs",
  "po-estimates": "estimates",
} as const satisfies Record<string, PoTab>;
const PO_VIEW_BY_TAB = {
  orders: "po-builder",
  rfqs: "po-rfqs",
  estimates: "po-estimates",
} as const;

/** Current pathname+search — what formatRoute() output is compared against for push dedupe. */
const currentUrl = () => window.location.pathname + window.location.search;

/** Project a rich FormPrefill down to its URL-shareable fields (S5 `values` stay in memory). */
function toRoutePrefill(p: FormPrefill): RoutePrefill | undefined {
  const rp: RoutePrefill = {};
  if (p.jobId) rp.jobId = p.jobId;
  if (p.parentCode) rp.parentCode = p.parentCode;
  if (p.variantCode) rp.variantCode = p.variantCode;
  if (p.workDate) rp.workDate = p.workDate;
  return Object.keys(rp).length > 0 ? rp : undefined;
}

/**
 * Admin-scoped session guard. Mounts the 30-min idle-logout + keep-alive (useIdleLogout)
 * ONLY for admins — rendered conditionally so the hook never runs for a submitter (who
 * keeps the 90-day session). `editing` (a dirty editor draft is open, reported up by
 * Accounts/Forms) adds the bounded wall-clock keep-alive. Renders nothing.
 */
function AdminSessionGuard({ editing }: { editing: boolean }) {
  const { logout } = useAuth();
  useIdleLogout(logout, editing);
  return null;
}

/**
 * Unified shell (P1). Every account — submitter or admin — lands on the SAME HomePage;
 * the action cards and the views they open are capability-gated (migration 0013). An
 * admin sees the same form cards as a field PM PLUS their management cards (Accounts /
 * Forms), and KEEPS submit-as: "Submit a form" routes to FormFillPage, whose "filled
 * out as" account selector still renders for admins. Every action is independently
 * re-gated server-side (requireRole / requireCapability); the SPA gating is convenience,
 * never the boundary (Invariant 2). Job Tracker / Equipment / Personnel / Materials views
 * mount here as their phases ship.
 *
 * G2.5 — real URL router (supersedes the R3 minimal history shim; deferred item #9 done).
 * The URL is now the navigation source of truth: src/lib/router.ts maps every top-level
 * view to a PATH-BASED deep link (/jobs/JOB-000018, /tasks/daily, /submit?job=…&form=…),
 * served cold by the Worker's existing SPA fallback (wrangler.jsonc
 * not_found_handling: "single-page-application" — zero Worker changes). Semantics:
 *   • In-app navigation pushes the destination URL (navigate); within-page state the
 *     pages report up (Job Tracker selected job, My Tasks tab) syncs the URL WITHOUT a
 *     re-render — push for "deeper" (open a job's detail), replace for tab flips and
 *     in-page back (syncRouteUp).
 *   • popstate parses the browser's restored URL and remounts the routed page fresh
 *     (popEpoch in the page keys) — back/forward are natural, and a popped
 *     /jobs/JOB-000018 entry revives THAT job (the URL now encodes it; R7's
 *     "pop lands on the plain list" note predates URL-encoded jobs).
 *   • The R3 dirty-guard is preserved EXACTLY: a dirty FormFillPage (onDirtyChange →
 *     formDirtyRef) confirms before a popstate discard; declining re-pushes the fill URL.
 *   • Cold-loading any URL lands there AFTER auth: while signed out the LoginPage renders
 *     and the URL is left untouched, so the intended destination survives the login.
 *     Unknown or unauthorized URLs render Home and normalize the address bar — never a
 *     blank page. Capability gating is driven by router.VIEW_CAPS (one source, no drift).
 * R3 — returnTo (unchanged in spirit, now a full route): openForm captures the route it
 * fired from, so FormFillPage's Submitted screen / pre-submit back land on the origin
 * (My Tasks — including its tab), not Home.
 */
export function App() {
  const { user, loading } = useAuth();
  // The routed location. Initialized from the address bar so a cold deep link lands
  // directly; unrecognized paths fall home (the mount effect below normalizes the URL).
  const [route, setRoute] = useState<AppRoute>(() => parseRoute(window.location) ?? HOME_ROUTE);
  // A dirty editor (Accounts/Forms) is open — drives the admin keep-alive (see AdminSessionGuard).
  const [editing, setEditing] = useState(false);
  // The S5 rollup draft (FormRenderer FormValues) for a deep-linked fill — far too large for a
  // URL, so it rides in memory next to the route's shareable prefill projection. Cleared on any
  // navigation away from the fill (a cold-loaded /submit URL simply has no draft — by design).
  const [formValues, setFormValues] = useState<FormPrefill["values"]>(undefined);
  // R3: the route a form deep-link fired FROM (captured inside openForm — callers pass nothing).
  // null = the fill wasn't deep-linked (home-card flow) → FormFillPage keeps its default screens.
  const [returnTo, setReturnTo] = useState<AppRoute | null>(null);
  // R7: a one-shot job preselect for the Job Tracker (deep links, the My Tasks "Log time" quick
  // action, and popped /jobs/:id history entries). The `key` on FieldOpsJobTracker re-mounts it
  // when the target changes; the tracker's own in-page navigation syncs the URL upward instead
  // (syncRouteUp), which deliberately does NOT touch this state — no remount, no refetch.
  const [jobTrackerJob, setJobTrackerJob] = useState<string | null>(
    () => {
      const r = parseRoute(window.location);
      return r?.view === "fieldops-jobs" ? (r.jobId ?? null) : null;
    },
  );
  // Bumped on every popstate: keyed into the routed pages so a history traversal always
  // remounts the page fresh from the URL (a popped entry is authoritative, never stale UI).
  const [popEpoch, setPopEpoch] = useState(0);
  // Synchronous mirror of the live route for the (stable) popstate listener, push dedupe,
  // and returnTo capture. syncRouteUp updates it without a re-render (see below).
  const routeRef = useRef<AppRoute>(route);
  // R3: FormFillPage's unsaved-input flag, consulted by the popstate guard before discarding.
  const formDirtyRef = useRef(false);

  // G2.5 history integration: canonicalize the entry URL, then restore routes on popstate.
  // Registered once; reads state through refs so the listener never goes stale.
  useEffect(() => {
    window.history.replaceState(null, "", formatRoute(routeRef.current));
    const onPop = () => {
      // The browser has already moved the address bar; parse it (unknown → home + normalize).
      const parsed = parseRoute(window.location);
      const target = parsed ?? HOME_ROUTE;
      if (routeRef.current.view === "fill" && formDirtyRef.current) {
        if (!window.confirm(DISCARD_PROMPT)) {
          // Stay on the form: re-push the fill URL the pop consumed (R3 semantics, exact).
          window.history.pushState(null, "", formatRoute(routeRef.current));
          return;
        }
        formDirtyRef.current = false;
      }
      if (parsed === null) window.history.replaceState(null, "", formatRoute(target));
      // Any traversal drops the in-memory fill payload + captured origin: the popped URL is
      // the whole state (a popped /submit?… entry re-seeds its prefill from the URL alone).
      setFormValues(undefined);
      setReturnTo(null);
      setEditing(false);
      routeRef.current = target;
      setJobTrackerJob(target.view === "fieldops-jobs" ? (target.jobId ?? null) : null);
      setRoute(target);
      setPopEpoch((e) => e + 1);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Never land on a view the account can't see (or on /login while signed in): render falls
  // back to Home immediately (below), and this normalizes the address bar to match.
  const cap = VIEW_CAPS[route.view];
  const blocked =
    !!user && (route.view === "login" || (cap !== null && !user.capabilities.includes(cap)));
  useEffect(() => {
    if (!blocked) return;
    window.history.replaceState(null, "", formatRoute(HOME_ROUTE));
    routeRef.current = HOME_ROUTE;
    setJobTrackerJob(null);
    setRoute(HOME_ROUTE);
  }, [blocked]);

  if (loading) {
    return <div className="centered muted">Loading…</div>;
  }
  if (!user) {
    // The address bar keeps the intended destination (e.g. a texted /jobs/JOB-000018):
    // once login succeeds, `route` — parsed from that same URL — renders it directly.
    return <LoginPage />;
  }

  const caps = user.capabilities;
  const has = (c: string) => caps.includes(c);
  // Capability gate for the CURRENT route — same map the URL-entry normalizer uses.
  const allowed = cap === null || has(cap);

  // Every in-app top-level navigation goes through here: push the destination URL (deduped
  // against the current address) and apply the route. `replace` for normalizations.
  const navigate = (next: AppRoute, replace = false) => {
    const url = formatRoute(next);
    if (url !== currentUrl()) {
      if (replace) window.history.replaceState(null, "", url);
      else window.history.pushState(null, "", url);
    }
    routeRef.current = next;
    setJobTrackerJob(next.view === "fieldops-jobs" ? (next.jobId ?? null) : null);
    setRoute(next);
  };
  // Within-page state reported UP by a mounted page (Job Tracker selected job, My Tasks tab):
  // sync the address bar + routeRef WITHOUT setState — the page already shows the new state,
  // so a re-render/remount would only refetch. push = "deeper" step, replace = tab flip/back.
  const syncRouteUp = (next: AppRoute, replace: boolean) => {
    const url = formatRoute(next);
    if (url !== currentUrl()) {
      if (replace) window.history.replaceState(null, "", url);
      else window.history.pushState(null, "", url);
    }
    routeRef.current = next;
  };

  const home = () => {
    setEditing(false);
    setFormValues(undefined);
    setReturnTo(null);
    navigate(HOME_ROUTE);
  };
  const backNav = <BackHomeNav onHome={home} />;
  // Open FormFillPage pre-filled from a checklist deep-link (P4 S4). The URL carries the
  // shareable projection (/submit?job=…&form=…&date=…); the S5 draft values ride in memory.
  // The keyed remount (see `key` below) guarantees the prefill seeds initial state even if
  // FormFillPage was already mounted. R3: capture the route this fired from — the round-trip
  // return target (no caller changes needed) — now tab/job-exact since routes encode them.
  const openForm = (p: FormPrefill) => {
    setEditing(false);
    setFormValues(p.values);
    if (routeRef.current.view !== "fill") setReturnTo(routeRef.current);
    navigate({ view: "fill", prefill: toRoutePrefill(p) });
  };
  // R3: the deep-link round trip back to the captured origin route.
  const returnLabel = returnTo?.view === "fieldops-tasks" ? "Back to My Tasks" : "Back";
  const returnToOrigin = () => {
    const dest = returnTo ?? HOME_ROUTE;
    setEditing(false);
    setFormValues(undefined);
    setReturnTo(null);
    navigate(dest);
  };
  // R7: open the Job Tracker, optionally preselecting a job's detail (My Tasks "Log time" quick
  // action / job-group links). No job → the plain list. The URL gets the job id — shareable.
  const openJobTracker = (jobId?: string) => {
    setEditing(false);
    navigate({ view: "fieldops-jobs", jobId });
  };
  // PR2: the per-job Materials page. Reached from the Job Tracker's expected-materials section
  // and from the daily field report's material-receipt region — both deep links, no home card
  // (the page is job-scoped, so there is no job-less entry point to put on the home grid).
  // 0067: the per-job weekly-report office screen, reached from the Job Tracker beside Materials.
  const openWeeklyReport = (jobId: string) => {
    navigate({ view: "fieldops-weekly-report", jobId });
  };
  const openJobMaterials = (jobId: string) => {
    setEditing(false);
    navigate({ view: "fieldops-job-materials", jobId });
  };
  // ADR-0006 PR-4: the per-job Schedule page, reached from the Job Tracker's card beside
  // Materials — a deep link, no home card (job-scoped, so there is no job-less entry point).
  const openJobSchedule = (jobId: string) => {
    setEditing(false);
    navigate({ view: "fieldops-job-schedule", jobId });
  };

  // The effective FormFillPage prefill: the route's shareable fields + the in-memory S5 draft.
  let fillPrefill: FormPrefill | undefined;
  if (route.view === "fill" && (route.prefill || formValues)) {
    fillPrefill = { ...route.prefill, values: formValues };
  }

  let page: ReactNode;
  if (route.view === "fill") {
    page = (
      <FormFillPage
        key={`${popEpoch}:${fillPrefill ? `${fillPrefill.jobId ?? ""}|${fillPrefill.parentCode ?? ""}|${fillPrefill.variantCode ?? ""}|${fillPrefill.workDate ?? ""}` : "blank"}`}
        onBack={home}
        tabBar={backNav}
        prefill={fillPrefill}
        returnTo={returnTo !== null ? { label: returnLabel, onReturn: returnToOrigin } : undefined}
        onDirtyChange={(d) => {
          formDirtyRef.current = d;
        }}
      />
    );
  } else if (route.view === "request") {
    page = <FormRequestPage onBack={home} />;
  } else if (route.view === "accounts" && allowed) {
    page = <AccountsPage tabBar={backNav} onEditingChange={setEditing} />;
  } else if (route.view === "forms" && allowed) {
    page = <FormsPage tabBar={backNav} onEditingChange={setEditing} />;
  } else if (route.view === "fieldops-jobs" && allowed) {
    // R7: keyed by the deep-link target (one-shot consumption) + popEpoch (a history pop
    // remounts fresh from the URL — including back INTO a job detail, which the URL encodes).
    page = (
      <FieldOpsJobTracker
        key={`${popEpoch}:${jobTrackerJob ?? "list"}`}
        onBack={home}
        initialJobId={jobTrackerJob}
        onJobViewChange={(id) =>
          syncRouteUp({ view: "fieldops-jobs", jobId: id ?? undefined }, id === null)
        }
        onOpenMaterials={has("cap.materials.receive") ? openJobMaterials : undefined}
        onOpenSchedule={has("cap.jobtracker.read") ? openJobSchedule : undefined}
        onOpenWeeklyReport={has("cap.jobtracker.manage") ? openWeeklyReport : undefined}
      />
    );
  } else if (route.view === "fieldops-weekly-report" && allowed) {
    page = (
      <WeeklyReportPage
        key={`${popEpoch}:${route.jobId}`}
        jobId={route.jobId}
        onBack={() => navigate({ view: "fieldops-jobs", jobId: route.jobId })}
        onHome={home}
        onOpenSchedule={has("cap.jobtracker.read") ? openJobSchedule : undefined}
      />
    );
  } else if (route.view === "fieldops-job-materials" && allowed) {
    page = (
      <JobMaterialsPage
        key={`${popEpoch}:${route.jobId}`}
        jobId={route.jobId}
        onHome={home}
        onOpenJob={openJobTracker}
        onOpenSchedule={has("cap.jobtracker.read") ? openJobSchedule : undefined}
      />
    );
  } else if (route.view === "fieldops-job-schedule" && allowed) {
    page = (
      <JobSchedulePage
        key={`${popEpoch}:${route.jobId}`}
        jobId={route.jobId}
        onHome={home}
        onOpenJob={openJobTracker}
        onOpenMaterials={has("cap.materials.receive") ? openJobMaterials : undefined}
        onOpenWeeklyReport={has("cap.jobtracker.manage") ? openWeeklyReport : undefined}
      />
    );
  } else if (route.view === "fieldops-tasks" && allowed) {
    page = (
      <FieldOpsMyTasks
        key={`${popEpoch}:tasks`}
        onBack={home}
        onOpenForm={openForm}
        onOpenJob={has("cap.jobtracker.read") ? openJobTracker : undefined}
        onOpenMaterials={has("cap.materials.receive") ? openJobMaterials : undefined}
        initialTab={route.tab}
        onTabChange={(t) => syncRouteUp({ view: "fieldops-tasks", tab: t }, true)}
      />
    );
  } else if (route.view === "fieldops-site-tasks" && allowed) {
    page = (
      <SiteTasksPage
        key={`${popEpoch}:site-tasks`}
        jobId={route.jobId}
        onBack={home}
        onOpenSchedule={has("cap.jobtracker.read") ? openJobSchedule : undefined}
        onJobChange={(j) => syncRouteUp({ view: "fieldops-site-tasks", jobId: j ?? undefined }, true)}
      />
    );
  } else if (route.view === "fieldops-inspections" && allowed) {
    page = <FieldOpsInspections onBack={home} />;
  } else if (route.view === "fieldops-equipment" && allowed) {
    page = <FieldOpsEquipment onBack={home} />;
  } else if (route.view === "fieldops-personnel" && allowed) {
    page = <FieldOpsPersonnel onBack={home} />;
  } else if (route.view === "materials-catalog" && allowed) {
    page = <MaterialsCatalogPage onBack={home} />;
  } else if (
    (route.view === "po-builder" || route.view === "po-rfqs" || route.view === "po-estimates") &&
    allowed
  ) {
    // One SHARED branch for the three PO-hub routes: React sees the same element type at the
    // same position across tab navigation, so the hub instance (and its mounted-panel wizard
    // state) survives — a per-view branch would remount and wipe a half-built PO on tab flip.
    page = (
      <PurchaseOrdersPage
        tab={PO_TAB_BY_VIEW[route.view]}
        onTabChange={(t) => navigate({ view: PO_VIEW_BY_TAB[t] }, true)}
        onBack={home}
      />
    );
  } else if (route.view === "po-vendors" && allowed) {
    page = <PoVendorsPage onBack={home} />;
  } else if (route.view === "po-config" && allowed) {
    page = <PoConfigPage onBack={home} />;
  } else if (route.view === "subcontractors" && allowed) {
    page = <SubcontractorsPage onBack={home} />;
  } else if (route.view === "subcontract-builder" && allowed) {
    page = <SubcontractBuilderPage onBack={home} />;
  } else {
    page = (
      <HomePage
        onNavigate={(v: HomeNav) => {
          setEditing(false);
          navigate({ view: v } as AppRoute); // every HomeNav is a param-less route entry
        }}
      />
    );
  }

  return (
    <>
      {user.role === "admin" && <AdminSessionGuard editing={editing} />}
      {/* Only the lazy admin views ever suspend; eager field views render straight through. */}
      <ChunkBoundary>
        <Suspense fallback={<div className="centered muted">Loading…</div>}>{page}</Suspense>
      </ChunkBoundary>
    </>
  );
}
