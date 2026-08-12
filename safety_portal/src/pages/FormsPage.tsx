import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { AppHeader } from "../components/AppHeader";
import { FormEditor } from "../components/FormEditor";
import { PublishMonitor } from "../components/PublishMonitor";
import { DEFAULT_WORKFLOW, formCatalog, getDefinition, WORKFLOWS_ORDERED, workflowLabel } from "../forms/registry";
import { FormRenderer, initialValues, type FormValues } from "../forms/FormRenderer";
import type { FormDefinition } from "../forms/types";
import {
  blankDefinition,
  formCodeFor,
  toClonedDraft,
  toEditDraft,
} from "../forms/editorModel";
import { validateDraft, checkParentGrouping } from "../forms/editorValidation";
import * as api from "../lib/api";
import { useAuth } from "../lib/auth";
import { type EditorMode, clearDraft, loadDraft, saveDraft } from "../lib/draftCache";

/**
 * Admin "Forms" tab — the form catalog manager (Phase-2). VIEW mode lists every active
 * form from the git catalog manifest grouped by parent and previews the selected form
 * with the REAL SPA FormRenderer (render-parity confidence in-tab). EDITOR mode is the
 * B8 sectioned builder: create-from-blank, edit (version bump), add-version (clone to a
 * new identity), and retire (delete) — each composing a FormDefinition from the closed
 * vocabulary, validated client-side and PUBLISHED via the send-free enqueue
 * (/api/admin/publish; the Mac daemon is the sole actuator). A live FormRenderer preview
 * sits beside the builder. Every publish is re-gated + re-validated server-side; this tab
 * never writes a form file.
 *
 * Rollback is DEFERRED (see the report) — the publish contract accepts op:"rollback" but
 * the editor surfaces only create/edit/add_version/delete this slice.
 */

type Mode = { kind: "view" } | EditorMode;

type Banner = { kind: "ok" | "err"; msg: string } | null;

/** Derive the identity (parent-vN stripped) from a form_code: jha-v2 → jha. */
function identityFromCode(formCode: string): string {
  return formCode.replace(/-v[0-9]+$/, "");
}

export function FormsPage({
  tabBar,
  onEditingChange,
}: {
  tabBar: ReactNode;
  onEditingChange?: (editing: boolean) => void;
}) {
  const catalog = useMemo(() => formCatalog(), []);
  const { user } = useAuth();
  const username = user?.username ?? "";

  // Flatten the parent→variant catalog into a selectable, parent-grouped list.
  const items = useMemo(() => {
    const out: { form_code: string; parent: string; label: string }[] = [];
    for (const p of catalog) {
      if (p.variants.length === 0 && p.form_code) {
        out.push({ form_code: p.form_code, parent: p.name, label: p.name });
      } else {
        for (const v of p.variants) {
          out.push({ form_code: v.form_code, parent: p.name, label: v.variant_label });
        }
      }
    }
    return out;
  }, [catalog]);

  const knownParents = useMemo(
    () => Array.from(new Set(catalog.map((p) => p.parent_form_code))).sort(),
    [catalog],
  );

  const [selected, setSelected] = useState<string>(items[0]?.form_code ?? "");
  const [mode, setMode] = useState<Mode>({ kind: "view" });

  // ── VIEW-mode preview state (mirrors the prior read-only viewer) ──────────────
  const viewDef = mode.kind === "view" && selected ? getDefinition(selected) : null;
  const [viewValues, setViewValues] = useState<FormValues>({});
  useEffect(() => {
    if (mode.kind !== "view") return;
    const d = selected ? getDefinition(selected) : null;
    setViewValues(d ? initialValues(d) : {});
  }, [selected, mode.kind]);

  // ── EDITOR-mode draft state ───────────────────────────────────────────────────
  const [draft, setDraft] = useState<FormDefinition | null>(null);
  const [identity, setIdentity] = useState("");
  const [parent, setParent] = useState("");
  const [category, setCategory] = useState<string>(DEFAULT_WORKFLOW);
  const [banner, setBanner] = useState<Banner>(null);
  const [busy, setBusy] = useState(false);
  const [refreshSignal, setRefreshSignal] = useState(0);

  // Preview fill-state for the editor draft — re-initialised whenever the section
  // structure changes (keyed on form_code + section count so retitling doesn't reset it).
  const [draftValues, setDraftValues] = useState<FormValues>({});
  const draftShape = draft ? `${draft.sections.length}:${draft.sections.map((s) => s.type).join(",")}` : "";
  useEffect(() => {
    if (draft) setDraftValues(initialValues(draft));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftShape]);

  function startCreate() {
    const d = blankDefinition();
    setDraft(d);
    setIdentity("");
    setParent("");
    setCategory(DEFAULT_WORKFLOW);
    setBanner(null);
    setMode({ kind: "create" });
  }

  function startEdit(sourceCode: string) {
    const src = getDefinition(sourceCode);
    if (!src) return;
    const id = identityFromCode(sourceCode);
    const d = toEditDraft(src, id);
    setDraft(d);
    setIdentity(id);
    setParent(src.parent_form_code);
    setCategory(catalog.find((p) => p.parent_form_code === src.parent_form_code)?.category ?? DEFAULT_WORKFLOW);
    setBanner(null);
    setMode({ kind: "edit", sourceCode, identity: id });
  }

  function startAddVersion(sourceCode: string) {
    const src = getDefinition(sourceCode);
    if (!src) return;
    // Clone to a NEW identity — pre-fill a "-copy" suffix the admin renames.
    const newIdentity = `${identityFromCode(sourceCode)}-copy`;
    const d = toClonedDraft(src, newIdentity, src.parent_form_code);
    setDraft(d);
    setIdentity(newIdentity);
    setParent(src.parent_form_code);
    setCategory(catalog.find((p) => p.parent_form_code === src.parent_form_code)?.category ?? DEFAULT_WORKFLOW);
    setBanner(null);
    setMode({ kind: "add_version", sourceCode });
  }

  // Cancel exits the editor but KEEPS the cached draft (recoverable on reopen) — only Discard
  // or a successful publish clears it.
  function cancelEditor() {
    setDraft(null);
    setBanner(null);
    setMode({ kind: "view" });
  }

  function discardDraft() {
    if (!window.confirm("Discard this draft? This can't be undone.")) return;
    clearDraft(username);
    setDraft(null);
    setBanner({ kind: "ok", msg: "Draft discarded." });
    setMode({ kind: "view" });
  }

  // Restore a cached in-progress draft ONCE per mount — this is what survives the admin idle
  // logout / reload (the draft otherwise lives only in the state cleared on unmount).
  const didRestore = useRef(false);
  useEffect(() => {
    if (didRestore.current || !username) return; // wait for the session, then attempt once
    didRestore.current = true;
    if (draft || mode.kind !== "view") return; // already editing — don't clobber
    const cached = loadDraft(username);
    if (!cached) return;
    setDraft(cached.draft);
    setIdentity(cached.identity);
    setParent(cached.parent);
    setCategory(cached.category ?? DEFAULT_WORKFLOW);
    setMode(cached.mode);
    setBanner({ kind: "ok", msg: "Restored your unsaved draft — use Discard to start over." });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [username]);

  // Auto-save the draft (per account) on every editor change, so nothing is lost on a timeout.
  useEffect(() => {
    if (!username || !draft || mode.kind === "view") return;
    saveDraft(username, { mode, draft, identity, parent, category });
  }, [username, draft, identity, parent, mode]);

  // Keep form_code + parent_form_code on the draft in lockstep with the identity/version
  // inputs (form_code is DERIVED, never typed — the server enforces this too).
  useEffect(() => {
    if (!draft) return;
    const wantCode = identity ? formCodeFor(identity, draft.version) : "";
    if (draft.form_code !== wantCode || draft.parent_form_code !== parent) {
      setDraft({ ...draft, form_code: wantCode, parent_form_code: parent });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, parent, draft?.version]);

  const editorOp: api.PublishOp | null =
    mode.kind === "create" ? "create" : mode.kind === "edit" ? "edit" : mode.kind === "add_version" ? "add_version" : null;

  const clientErrors = useMemo(() => {
    if (!draft || !editorOp) return [];
    const errs = validateDraft(draft, { identity, parentFormCode: parent });
    // create/add_version add a NEW form to the parent → guard the catalog grouping rule
    // up front (edit bumps an existing identity, so its grouping is unchanged). This is
    // the rule that previously only surfaced at the daemon as a failed publish.
    if (editorOp === "create" || editorOp === "add_version") {
      const g = checkParentGrouping(catalog, parent, draft.variant_label ?? null);
      if (g) errs.push(g);
    }
    return errs;
  }, [draft, editorOp, identity, parent, catalog]);

  async function onPublish() {
    if (!draft || !editorOp) return;
    setBusy(true);
    setBanner(null);
    try {
      await api.publishForm({
        op: editorOp,
        identity,
        parent_form_code: parent,
        definition: draft,
        category,
      });
      setBanner({ kind: "ok", msg: `Publish queued for ${draft.form_code}. Track it below.` });
      setRefreshSignal((n) => n + 1);
      setMode({ kind: "view" });
      setDraft(null);
      clearDraft(username); // form is queued → the draft is done; don't re-restore it.
    } catch (e) {
      setBanner({ kind: "err", msg: explainPublish(e) });
    } finally {
      setBusy(false);
    }
  }

  async function onRetire(formCode: string) {
    const id = identityFromCode(formCode);
    const src = getDefinition(formCode);
    if (!window.confirm(`Retire ${formCode}? It will be removed from the active picker (filed submissions still render).`)) {
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      await api.publishForm({
        op: "delete",
        identity: id,
        parent_form_code: src?.parent_form_code ?? id,
        target_form_code: formCode,
      });
      setBanner({ kind: "ok", msg: `Retire queued for ${formCode}. Track it below.` });
      setRefreshSignal((n) => n + 1);
    } catch (e) {
      setBanner({ kind: "err", msg: explainPublish(e) });
    } finally {
      setBusy(false);
    }
  }

  // Move a form TYPE between workflows (parent-wide). The backend `recategorize` op flips the
  // catalog parent's category — every form/variant under it moves together. Queued like a
  // publish; takes effect after the daemon commits + the catalog reloads.
  async function onRecategorize(parentFormCode: string, newCategory: string) {
    const current =
      catalog.find((p) => p.parent_form_code === parentFormCode)?.category ?? DEFAULT_WORKFLOW;
    if (newCategory === current) return;
    if (
      !window.confirm(
        `Move the "${parentFormCode}" form type to the ${workflowLabel(newCategory)} workflow? ` +
          "All its forms move together.",
      )
    ) {
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      await api.publishForm({
        op: "recategorize",
        identity: parentFormCode,
        parent_form_code: parentFormCode,
        category: newCategory,
      });
      setBanner({ kind: "ok", msg: `Workflow change queued for ${parentFormCode}. Track it below.` });
      setRefreshSignal((n) => n + 1);
    } catch (e) {
      setBanner({ kind: "err", msg: explainPublish(e) });
    } finally {
      setBusy(false);
    }
  }

  // Re-open a FAILED publish in the editor (its composed definition is saved in the queue
  // row) so the admin can fix what tripped it and re-publish, instead of rebuilding it.
  async function editFailedPublish(id: number) {
    try {
      const r = await api.fetchPublishRequest(id);
      if (!r.definition_json) {
        setBanner({ kind: "err", msg: "That request has no saved form to edit." });
        return;
      }
      const def = JSON.parse(r.definition_json) as FormDefinition;
      setDraft(def);
      setIdentity(r.identity);
      setParent(r.parent_form_code);
      setCategory(r.category ?? DEFAULT_WORKFLOW);
      setBanner({ kind: "ok", msg: `Loaded the failed publish for ${r.identity} — fix it and re-publish.` });
      if (r.op === "edit") setMode({ kind: "edit", sourceCode: def.form_code, identity: r.identity });
      else if (r.op === "add_version") setMode({ kind: "add_version", sourceCode: def.form_code });
      else setMode({ kind: "create" });
    } catch {
      setBanner({ kind: "err", msg: "Could not load that failed publish." });
    }
  }

  const inEditor = mode.kind === "create" || mode.kind === "edit" || mode.kind === "add_version";

  // Report a dirty editor (open + unsaved draft) up to the admin shell so useIdleLogout switches
  // to its wall-clock keep-alive and never bounces work-in-progress. Reset to false on unmount
  // (tab switch) so a left-behind draft doesn't pin the shell "editing" and silently disable the
  // proactive idle logout on the other tabs — the draft itself stays localStorage-cached and is
  // restored on the next editor open.
  useEffect(() => {
    onEditingChange?.(inEditor && !!draft);
  }, [inEditor, draft, onEditingChange]);
  useEffect(() => () => onEditingChange?.(false), [onEditingChange]);

  return (
    <div className="page">
      <AppHeader />
      {tabBar}
      <main className="page__main">
        {banner ? (
          <p className={banner.kind === "ok" ? "banner banner--ok" : "banner banner--err"} role="status">
            {banner.msg}
          </p>
        ) : null}

        {inEditor && draft ? (
          // ── EDITOR LAYOUT: builder on the left, live preview on the right ──────
          <>
            <div className="form-editor__toolbar">
              <h1 className="page__heading">
                {mode.kind === "create"
                  ? "New form"
                  : mode.kind === "edit"
                    ? `Edit ${mode.identity} → v${draft.version}`
                    : "Add version (clone)"}
              </h1>
              <div className="jha__actions" style={{ marginTop: 0 }}>
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={busy || clientErrors.length > 0}
                  onClick={() => void onPublish()}
                >
                  {busy ? "Publishing…" : "Publish"}
                </button>
                <button type="button" className="btn btn--secondary" disabled={busy} onClick={cancelEditor}>
                  Cancel
                </button>
                <button type="button" className="btn btn--danger" disabled={busy} onClick={discardDraft}>
                  Discard draft
                </button>
              </div>
            </div>

            {clientErrors.length > 0 ? (
              <div className="form-editor__errors" role="alert">
                <p className="form-editor__errors-title">Fix before publishing:</p>
                <ul>
                  {clientErrors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="banner banner--ok" role="status">Ready to publish — no client-side errors.</p>
            )}

            <div className="form-editor__split">
              <div>
                <FormEditor
                  def={draft}
                  onChange={setDraft}
                  mode={mode.kind}
                  identity={identity}
                  onIdentityChange={setIdentity}
                  parentFormCode={parent}
                  onParentChange={setParent}
                  knownParents={knownParents}
                  category={category}
                  onCategoryChange={setCategory}
                />
              </div>
              <div className="form-editor__preview-pane">
                <h2 className="form-editor__sub-heading">Live preview</h2>
                <div className="card forms-mgr__preview" aria-label="Live preview">
                  <FormRenderer def={draft} values={draftValues} setValues={setDraftValues} />
                </div>
              </div>
            </div>
          </>
        ) : (
          // ── VIEW LAYOUT: catalog list + read-only preview + per-form actions ──
          <>
            <div className="form-editor__toolbar">
              <h1 className="page__heading">Forms</h1>
              <button type="button" className="btn btn--primary" onClick={startCreate}>
                + New form
              </button>
            </div>
            <div className="forms-mgr">
              <aside className="forms-mgr__list" aria-label="Form catalog">
                <h2 className="forms-mgr__heading">
                  Catalog <span className="forms-mgr__count">{items.length}</span>
                </h2>
                <ul className="forms-mgr__items">
                  {items.map((it) => (
                    <li key={it.form_code}>
                      <button
                        type="button"
                        className={`forms-mgr__item${it.form_code === selected ? " forms-mgr__item--active" : ""}`}
                        aria-current={it.form_code === selected}
                        onClick={() => setSelected(it.form_code)}
                      >
                        <span className="forms-mgr__item-label">{it.label}</span>
                        <span className="forms-mgr__item-parent">{it.parent}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </aside>
              <section>
                {viewDef ? (
                  <>
                    <header className="forms-mgr__meta">
                      <h1 className="page__heading">{viewDef.form_name}</h1>
                      <dl className="forms-mgr__meta-grid">
                        <div><dt>Form code</dt><dd>{viewDef.form_code}</dd></div>
                        <div><dt>Parent</dt><dd>{viewDef.parent_form_code}</dd></div>
                        <div><dt>Variant</dt><dd>{viewDef.variant_label ?? "—"}</dd></div>
                        <div><dt>Version</dt><dd>v{viewDef.version}</dd></div>
                        <div><dt>Archetype</dt><dd>{viewDef.archetype}</dd></div>
                        <div><dt>Sections</dt><dd>{viewDef.sections.length}</dd></div>
                      </dl>
                      <div className="jha__actions" style={{ marginTop: 8 }}>
                        <button type="button" className="btn btn--edit" disabled={busy} onClick={() => startEdit(viewDef.form_code)}>
                          Edit (new version)
                        </button>
                        <button type="button" className="btn btn--edit" disabled={busy} onClick={() => startAddVersion(viewDef.form_code)}>
                          Add version (clone)
                        </button>
                        <button type="button" className="btn btn--retire" disabled={busy} onClick={() => void onRetire(viewDef.form_code)}>
                          Retire
                        </button>
                        <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <span className="muted">Workflow</span>
                          <select
                            className="field__input"
                            value={catalog.find((p) => p.parent_form_code === viewDef.parent_form_code)?.category ?? DEFAULT_WORKFLOW}
                            disabled={busy}
                            aria-label="Change workflow"
                            onChange={(e) => void onRecategorize(viewDef.parent_form_code, e.target.value)}
                          >
                            {WORKFLOWS_ORDERED.map((w) => (
                              <option key={w.id} value={w.id}>{w.label}</option>
                            ))}
                          </select>
                        </label>
                      </div>
                    </header>
                    <div className="forms-mgr__preview card" aria-label="Live preview">
                      <FormRenderer def={viewDef} values={viewValues} setValues={setViewValues} />
                    </div>
                  </>
                ) : (
                  <p className="muted">No forms in the catalog.</p>
                )}
              </section>
            </div>
          </>
        )}

        <PublishMonitor refreshSignal={refreshSignal} onEditFailed={(id) => void editFailedPublish(id)} />
      </main>
    </div>
  );
}

/** Map a publish error to an operator-readable message — surfacing the server `reason`, and
 *  NEVER falling through to a contentless message (the final branch names the code/status so a
 *  bare rejection is always explainable). Exported for unit coverage. */
export function explainPublish(e: unknown): string {
  if (e instanceof api.PublishError) {
    // All 401s on this route mean the admin session is no longer valid (the 30-minute idle
    // timeout is the common one) — the actionable fix is to sign in again.
    if (e.status === 401) return "Your admin session expired (30-minute idle timeout). Sign in again, then re-publish.";
    if (e.status === 409) return "Another publish for this form type is still in progress — wait for it to finish.";
    if (e.status === 403) return "You're not authorized to publish forms.";
    if (e.reason) return `Rejected: ${e.reason}`;
    if (e.code === "invalid_op") return "Invalid operation.";
    if (e.code === "invalid_identity") return "Invalid identity slug.";
    if (e.code === "invalid_parent_form_code") return "Invalid form type (parent).";
    if (e.code === "invalid_target_form_code") return "Invalid target form code.";
    if (e.code === "invalid_category") return "Unknown workflow — pick one from the list.";
    if (e.code === "bad_request") return "The publish request was malformed. Reload the editor and try again.";
    // No mapped reason/code — still tell the operator WHAT was rejected (never contentless).
    return `Publish was rejected (${e.code}${e.status ? `, HTTP ${e.status}` : ""}). Please review and try again.`;
  }
  return "Something went wrong while publishing. Please try again.";
}
