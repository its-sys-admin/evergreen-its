import { useRef } from "react";
import { PageShell } from "../components/PageShell";
import { PoBuilderPage } from "./PoBuilderPage";
import { RfqBuilderPage } from "./RfqBuilderPage";
import { EstimatesPage } from "./EstimatesPage";

// Purchase-Orders HUB (2026-07 fold): one shell, three tab panels — the PO builder/tracker,
// the RFQ composer, and the vendor-estimate importer. The procurement lifecycle reads left to
// right (order → request quotes → import the answers), and the two ADR-0004 surfaces stop
// being standalone home cards.
//
// Routing: the active tab IS the route — App maps po-builder/po-rfqs/po-estimates onto the
// `tab` prop and `onTabChange` navigates (replace semantics, the My Tasks tab-flip precedent),
// so /purchase-orders, /purchase-orders/rfqs, and /purchase-orders/estimates deep-link
// directly and cold-load onto the right tab. All three views share cap.po.manage (VIEW_CAPS),
// so a tab flip can never cross a capability boundary; the Worker re-gates every call anyway
// (Invariant 2 — SPA gating is convenience, never the boundary).
//
// Panels MOUNT ON FIRST VISIT and then stay mounted with `hidden` (the FieldOpsMyTasks
// pattern): each panel's fetches run once, and in-progress work — a half-built PO wizard, a
// half-composed RFQ — survives a glance at another tab. App renders this SAME component for
// all three routed views (one shared branch), so React keeps the instance across tab
// navigation; a remount would wipe the wizards.
//
// Cross-tab flows (the fold's point):
//   • Orders "New PO from a vendor estimate" → goReview(id) → the Estimates panel opens the
//     DISPOSITION screen — the ADR-0004 decision-3 fidelity gate (side-by-side source preview
//     + per-line accept) remains the ONLY estimate→PO path; this hub adds navigation, never a
//     bypass.
//   • A disposition import minted draft PO #N → onImported(N) → flip to Orders with the draft
//     OPEN IN THE BUILDER, still fully editable (add/modify lines, attachments, terms) before
//     Generate.

export type PoTab = "orders" | "rfqs" | "estimates";

const TABS: { key: PoTab; label: string }[] = [
  { key: "orders", label: "Purchase Orders" },
  { key: "rfqs", label: "RFQs" },
  { key: "estimates", label: "Vendor Estimates" },
];

export function PurchaseOrdersPage({
  tab,
  onTabChange,
  onBack,
  externalOpenDraft,
  onExternalOpenConsumed,
}: {
  tab: PoTab;
  onTabChange: (t: PoTab) => void;
  onBack: () => void;
  /** App-level one-shot (Track D2): open this draft in the builder — the job Procurement
   *  screen's just-created change order. Own nonce counter; adopted into the internal
   *  request stream below. `origin` picks the builder's copy (this channel also serves
   *  estimate imports internally). */
  externalOpenDraft?: { id: number; nonce: number; origin: "change_order" } | null;
  /** Report the adopted nonce so App clears its ref — WITHOUT this the hub's own dedupe ref
   *  resets on remount and a long-consumed request replays on every later visit. */
  onExternalOpenConsumed?: (nonce: number) => void;
}) {
  // Monotonic nonce for the cross-tab one-shot requests below: a fresh nonce re-fires the
  // consumer's effect even when the same id is requested twice (e.g. review, back out, review
  // again). Refs, not state — bumping one must not itself re-render.
  const nonceRef = useRef(0);
  const openDraftReqRef = useRef<{ id: number; nonce: number; origin?: "estimate" | "change_order" } | null>(null);
  const reviewReqRef = useRef<{ id: number; nonce: number } | null>(null);

  // Render-phase adoption of the external one-shot (idempotent under StrictMode double-render:
  // the second pass sees the nonce already recorded and skips). The render that carries the
  // new prop is itself the render that must hand the request to PoBuilderPage below. Consumption
  // is reported UP so the App-held request cannot outlive this mount and replay later.
  const lastExternalNonceRef = useRef(0);
  if (externalOpenDraft && externalOpenDraft.nonce !== lastExternalNonceRef.current) {
    lastExternalNonceRef.current = externalOpenDraft.nonce;
    openDraftReqRef.current = { id: externalOpenDraft.id, nonce: ++nonceRef.current, origin: externalOpenDraft.origin };
    onExternalOpenConsumed?.(externalOpenDraft.nonce);
  }

  // Mount-on-first-visit set. Mutated during render (idempotent add — safe under StrictMode
  // double-render): the tab prop change that reveals a new panel is itself the re-render that
  // mounts it, so no effect/extra render is needed.
  const visitedRef = useRef<Set<PoTab>>(new Set());
  visitedRef.current.add(tab);
  const visited = visitedRef.current;

  /** Orders tab picked a reviewable estimate → open its disposition on the Estimates tab. */
  const goReview = (estimateId: number) => {
    reviewReqRef.current = { id: estimateId, nonce: ++nonceRef.current };
    onTabChange("estimates");
  };
  /** Disposition minted a draft PO → open it in the builder on the Orders tab. */
  const goDraft = (poId: number) => {
    openDraftReqRef.current = { id: poId, nonce: ++nonceRef.current, origin: "estimate" };
    onTabChange("orders");
  };

  return (
    <PageShell onHome={onBack}>
      <h2 className="page__heading">Purchase Orders</h2>
      <nav className="admin-tabs" role="tablist" aria-label="Purchase-order lanes">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={`admin-tabs__tab${tab === t.key ? " admin-tabs__tab--active" : ""}`}
            onClick={() => onTabChange(t.key)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {visited.has("orders") && (
        <div hidden={tab !== "orders"} role="tabpanel" aria-label="Purchase Orders">
          <PoBuilderPage
            onReviewEstimate={goReview}
            onOpenEstimatesTab={() => onTabChange("estimates")}
            openDraftRequest={openDraftReqRef.current}
          />
        </div>
      )}
      {visited.has("rfqs") && (
        <div hidden={tab !== "rfqs"} role="tabpanel" aria-label="RFQs">
          <RfqBuilderPage />
        </div>
      )}
      {visited.has("estimates") && (
        <div hidden={tab !== "estimates"} role="tabpanel" aria-label="Vendor Estimates">
          <EstimatesPage
            reviewRequest={reviewReqRef.current}
            onImported={goDraft}
            onOpenPoTab={() => onTabChange("orders")}
          />
        </div>
      )}
    </PageShell>
  );
}
