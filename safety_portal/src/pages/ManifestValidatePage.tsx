import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../lib/fieldops_manifests";
import type {
  ManifestColumnMap,
  ManifestGridRow,
  ManifestListRow,
  ManifestPlanResponse,
  ManifestResolvedLine,
} from "../lib/fieldops_manifests";
import { errorText } from "../lib/errorCopy";

// ─────────────────────────────────────────────────────────────────────────────
// Manifest validate screen (PR3b) — the human half of "the parser proposes, the human disposes".
//
// A SUB-FACE of JobMaterialsPage (props + onClose, no PageShell), mounted `key={manifestId}` so
// switching manifests remounts rather than leaking the previous document's edits into the next.
//
// WHY THIS SCREEN EXISTS AT ALL. The daemon deliberately never picks a quantity column and never
// writes a material line. On a DELTA BOM there are seven quantity-ish columns and choosing is a
// domain judgement; the parser proposes the highest revision and SHOWS its arithmetic
// cross-check, and a person confirms. Everything here is built so that confirming is a real act
// rather than a rubber stamp:
//
//   • the source page sits beside the grid, so what is being confirmed is visible;
//   • AMBIGUOUS part numbers (a part matching more than one existing line) block the commit until
//     each is resolved individually — duplicates are universal in the real BOMs, so silently
//     picking a winner is the failure mode this screen exists to prevent;
//   • "Preview changes" is a real dry run against live data, not a local guess.
//
// Nothing here writes until Commit; Preview writes nothing at all.
// ─────────────────────────────────────────────────────────────────────────────

/** Concepts the importer understands, mirroring field_ops/manifest_parse.py's vocabulary. Only
 *  the ones an expected-material line can actually consume are offered as column targets — the
 *  rest of the parser's concepts (project, job_number, release, weight, …) are real but land
 *  nowhere, so offering them would imply an import that does not happen. */
const CONCEPT_LABELS: Record<string, string> = {
  ignore: "— not imported —",
  part_number: "Part number",
  description: "Description",
  category: "Category / grouping",
  qty: "Quantity",
  unit: "Unit",
  ship_date: "Ship date",
  delivery_date: "Delivery date",
};
const CONCEPT_ORDER = Object.keys(CONCEPT_LABELS);

/** `manifest_parse` flag → what a human should do about it. */
const FLAG_COPY: Record<string, string> = {
  qty_unparseable: "no readable quantity",
  orphan_continuation: "continuation row with no parent above it",
};

type RowState = {
  keep: boolean;
  /** Per-cell overrides, column index → edited text. Absent = use the source cell verbatim. */
  edits: Record<number, string>;
};

/** A line the office adds by hand because the document missed it. */
type AddedLine = { key: string; description: string; part_number: string; qty: string; unit: string };

function parseCells(raw: string): string[] {
  try {
    const v = JSON.parse(raw) as unknown;
    return Array.isArray(v) ? v.map((c) => String(c ?? "")) : [];
  } catch {
    return [];
  }
}

function parseColumnMap(raw: string | null): ManifestColumnMap | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as ManifestColumnMap;
  } catch {
    return null;
  }
}

function errText(e: unknown, fallback: string): string {
  if (e && typeof e === "object" && "code" in e) {
    const code = (e as { code: string | null }).code;
    if (code) return errorText(code);
  }
  return e instanceof Error && e.message ? e.message : fallback;
}

/** A quantity cell → a number the Worker will accept, or null. Tolerates thousands separators,
 *  matching the parser's own `parse_number`. */
function toQty(text: string): number | null {
  const t = text.trim().replace(/,/g, "");
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Normalize a date cell to the YYYY-MM-DD the Worker's `optDate` accepts; anything else is
 *  dropped rather than guessed at (an unparseable date must not become a wrong one). */
function toDate(text: string): string | null {
  const t = text.trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(t) ? t : null;
}

export function ManifestValidatePage({
  manifestId,
  onClose,
}: {
  manifestId: number;
  /** Leave the screen. `committed` tells the parent whether to reload its materials — a commit
   *  inserts expected-material lines, so the page behind this one is stale the moment we finish. */
  onClose: (notice?: { ok: boolean; text: string }, committed?: boolean) => void;
}) {
  const [manifest, setManifest] = useState<ManifestListRow | null>(null);
  const [detail, setDetail] = useState<{
    columnMap: ManifestColumnMap | null;
    headerMeta: Record<string, string>;
    notes: string[];
    previewPages: number[];
  } | null>(null);
  const [rows, setRows] = useState<ManifestGridRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Human decisions.
  const [concepts, setConcepts] = useState<Record<number, string>>({});
  const [qtyCol, setQtyCol] = useState<number | null>(null);
  const [rowState, setRowState] = useState<Record<number, RowState>>({});
  const [added, setAdded] = useState<AddedLine[]>([]);
  /** source_row_index → chosen existing line id, for part numbers matching MORE than one line. */
  const [ambiguousChoice, setAmbiguousChoice] = useState<Record<number, number>>({});
  const [mode, setMode] = useState<api.ManifestMode>("add_new");

  const [plan, setPlan] = useState<ManifestPlanResponse | null>(null);
  const [busy, setBusy] = useState<null | "plan" | "commit" | "discard">(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const [page, setPage] = useState(1);
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoadError(null);
    Promise.all([api.fetchManifest(manifestId), api.fetchAllManifestRows(manifestId)])
      .then(([d, r]) => {
        const cmap = parseColumnMap(d.manifest.column_map_json);
        let meta: Record<string, string> = {};
        try {
          meta = d.manifest.header_meta_json
            ? (JSON.parse(d.manifest.header_meta_json) as Record<string, string>)
            : {};
        } catch {
          meta = {};
        }
        setManifest(d.manifest);
        setDetail({
          columnMap: cmap,
          headerMeta: meta,
          notes: (d.manifest.parse_notes ?? "").split("\n").filter(Boolean),
          previewPages: d.preview_pages,
        });
        setRows(r);
        // Seed the human decisions FROM the parser's proposal — this is the "proposes" half.
        const seeded: Record<number, string> = {};
        for (const [concept, idx] of Object.entries(cmap?.mapping ?? {})) {
          if (concept in CONCEPT_LABELS) seeded[idx] = concept;
        }
        setConcepts(seeded);
        setQtyCol(cmap?.qty_default ?? cmap?.mapping?.qty ?? null);
        // Data + continuation rows start kept; header/meta/section rows are never importable.
        const st: Record<number, RowState> = {};
        for (const row of r) {
          st[row.row_index] = {
            keep: row.kind === "data" || row.kind === "continuation",
            edits: {},
          };
        }
        setRowState(st);
      })
      .catch((e) => setLoadError(errText(e, "Could not load this manifest.")));
  }, [manifestId]);

  useEffect(load, [load]);

  // Preview: fetch on page change. `key={page}` on the <img> is load-bearing elsewhere in this
  // codebase (without it React reuses the element and onLoad never re-fires); we key it too so a
  // page change genuinely swaps the image rather than silently keeping the old one.
  useEffect(() => {
    let cancelled = false;
    if (!detail?.previewPages.includes(page)) {
      setPreviewSrc(null);
      return;
    }
    api
      .fetchManifestPreview(manifestId, page)
      .then((p) => {
        if (!cancelled) setPreviewSrc(`data:image/png;base64,${p.png_b64}`);
      })
      .catch(() => {
        if (!cancelled) setPreviewSrc(null);
      });
    return () => {
      cancelled = true;
    };
  }, [manifestId, page, detail]);

  const width = useMemo(
    () => (rows ?? []).reduce((w, r) => Math.max(w, parseCells(r.cells_json).length), 0),
    [rows],
  );

  const headerRow = useMemo(() => (rows ?? []).find((r) => r.kind === "header") ?? null, [rows]);
  const headerCells = useMemo(
    () => (headerRow ? parseCells(headerRow.cells_json) : []),
    [headerRow],
  );
  const importableRows = useMemo(
    () => (rows ?? []).filter((r) => r.kind === "data" || r.kind === "continuation"),
    [rows],
  );

  /** Column index for a concept, from the HUMAN's current mapping (not the parser's proposal). */
  const colFor = useCallback(
    (concept: string): number | null => {
      const hit = Object.entries(concepts).find(([, c]) => c === concept);
      return hit ? Number(hit[0]) : null;
    },
    [concepts],
  );

  /** The current decisions → the lines a commit would author. This is the single place the grid
   *  becomes data; Preview and Commit both read it, so what you previewed IS what commits. */
  const resolvedLines = useMemo((): ManifestResolvedLine[] => {
    const cDesc = colFor("description");
    const cPart = colFor("part_number");
    const cCat = colFor("category");
    const cUnit = colFor("unit");
    const cShip = colFor("ship_date");
    const cDeliv = colFor("delivery_date");
    const cell = (r: ManifestGridRow, idx: number | null): string => {
      if (idx === null) return "";
      const st = rowState[r.row_index];
      if (st && idx in st.edits) return st.edits[idx];
      return parseCells(r.cells_json)[idx] ?? "";
    };
    const out: ManifestResolvedLine[] = [];
    for (const r of importableRows) {
      if (!rowState[r.row_index]?.keep) continue;
      const description = cell(r, cDesc).trim();
      const part = cell(r, cPart).trim();
      out.push({
        source_row_index: r.row_index,
        // The Worker refuses a line with neither material_id nor description
        // (`description_required`) rather than inventing one — so a part-number-only row surfaces
        // here as an empty Description the office must fill, not as a silent synthesis.
        description: description || null,
        part_number: part || null,
        category: cell(r, cCat).trim() || null,
        qty: toQty(cell(r, qtyCol)),
        unit: cell(r, cUnit).trim() || null,
        expected_date: toDate(cell(r, cDeliv)),
        expected_ship_date: toDate(cell(r, cShip)),
        material_id: null,
      });
    }
    // Hand-added lines ride after the document's own, keyed off the highest source index so their
    // watermark positions never collide with a real row's.
    const base = Math.max(0, ...importableRows.map((r) => r.row_index)) + 1;
    added.forEach((a, i) => {
      out.push({
        source_row_index: base + i,
        description: a.description.trim() || null,
        part_number: a.part_number.trim() || null,
        category: null,
        qty: toQty(a.qty),
        unit: a.unit.trim() || null,
        expected_date: null,
        expected_ship_date: null,
        material_id: null,
      });
    });
    return out;
  }, [importableRows, rowState, added, colFor, qtyCol]);

  const missingDescription = useMemo(
    () => resolvedLines.filter((l) => !l.description && !l.material_id).length,
    [resolvedLines],
  );
  const unresolvedAmbiguous = useMemo(
    () => (plan?.ambiguous ?? []).filter((a) => ambiguousChoice[a.source_row_index] === undefined),
    [plan, ambiguousChoice],
  );

  const status = manifest?.status;
  const committable = status === "parsed" || status === "committing";

  function setKeep(rowIndex: number, keep: boolean) {
    setRowState((s) => ({ ...s, [rowIndex]: { ...(s[rowIndex] ?? { edits: {} }), keep } }));
    setPlan(null); // a changed set invalidates the dry run
  }

  function setEdit(rowIndex: number, col: number, value: string) {
    setRowState((s) => {
      const cur = s[rowIndex] ?? { keep: true, edits: {} };
      return { ...s, [rowIndex]: { ...cur, edits: { ...cur.edits, [col]: value } } };
    });
    setPlan(null);
  }

  async function runPlan() {
    if (busy) return;
    setBusy("plan");
    setMsg(null);
    try {
      const p = await api.planManifest(manifestId, resolvedLines);
      setPlan(p);
      setAmbiguousChoice({});
    } catch (e) {
      setMsg({ ok: false, text: errText(e, "Could not preview the changes.") });
    } finally {
      setBusy(null);
    }
  }

  async function runCommit() {
    if (busy) return;
    setBusy("commit");
    setMsg(null);
    setProgress(null);
    try {
      const res = await api.commitAll(manifestId, mode, resolvedLines, (done, total) =>
        setProgress(`${done} of ${total} lines…`),
      );
      onClose(
        { ok: true, text: `Imported ${res.inserted} material line${res.inserted === 1 ? "" : "s"}.` },
        true,
      );
    } catch (e) {
      setMsg({ ok: false, text: errText(e, "The import didn't finish — nothing partial was left.") });
      setBusy(null);
      setProgress(null);
    }
  }

  async function runDiscard() {
    if (busy) return;
    setBusy("discard");
    setMsg(null);
    try {
      await api.discardManifest(manifestId);
      onClose({ ok: true, text: "Manifest discarded." }, false);
    } catch (e) {
      setMsg({ ok: false, text: errText(e, "Could not discard this manifest.") });
      setBusy(null);
    }
  }

  if (loadError) {
    return (
      <section className="card dash-section" aria-label="Manifest load error">
        <p className="dash-error">{loadError}</p>
        <button type="button" className="btn btn--secondary" onClick={() => onClose()}>
          ← Back to materials
        </button>
      </section>
    );
  }
  if (!manifest || !rows || !detail) {
    return (
      <section className="card dash-section" aria-label="Loading manifest">
        <p className="dash__intro">Loading the manifest…</p>
      </section>
    );
  }

  return (
    <>
      <div className="dash-row">
        <button type="button" className="btn btn--secondary" onClick={() => onClose()}>
          ← Back to materials
        </button>
        <span className="dash-pill">{manifest.status}</span>
      </div>
      <h3 className="page__heading">{manifest.filename}</h3>

      {msg ? (
        <p className={msg.ok ? "dash-ok" : "dash-error"} role="status">
          {msg.text}
        </p>
      ) : null}

      {!committable ? (
        <section className="card dash-section" aria-label="Manifest not ready">
          <p className="dash__intro">
            {manifest.status === "refused"
              ? `This manifest was refused${manifest.detail ? ` (${manifest.detail})` : ""} — ask the office for a different file.`
              : manifest.status === "committed"
                ? "This manifest has already been imported."
                : manifest.status === "discarded"
                  ? "This manifest was discarded."
                  : "This manifest is still being read. Refresh in a moment."}
          </p>
        </section>
      ) : null}

      {/* The editing surface exists ONLY for a manifest that can actually be imported. A refused
          or discarded one has no grid (the daemon never posted rows) and no action available, so
          rendering a disabled editor over an empty table would be noise pretending to be a choice.
          Wide branch uses minmax(0, …) + min-width:0 per pane — a bare fr pane will not shrink
          and overflows the page. */}
      {committable ? (
      <div
        className="manifest-validate__split"
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1.6fr)",
          gap: "1rem",
          alignItems: "start",
        }}
      >
        {/* ── LEFT: the source, so what is being confirmed is visible ─────────────────── */}
        <section className="card dash-section" style={{ minWidth: 0 }} aria-label="Source document">
          <h4>Source</h4>
          {detail.previewPages.length === 0 ? (
            <p className="dash__intro">
              No page preview for this file — spreadsheets have no page image. Check the grid
              against your own copy before importing.
            </p>
          ) : (
            <>
              <div className="dash-row">
                <button
                  type="button"
                  className="btn btn--secondary"
                  disabled={page <= Math.min(...detail.previewPages)}
                  onClick={() => setPage((p) => p - 1)}
                  aria-label="Previous source page"
                >
                  ‹ Prev
                </button>
                <span>
                  Page {page} of {Math.max(...detail.previewPages)}
                </span>
                <button
                  type="button"
                  className="btn btn--secondary"
                  disabled={page >= Math.max(...detail.previewPages)}
                  onClick={() => setPage((p) => p + 1)}
                  aria-label="Next source page"
                >
                  Next ›
                </button>
              </div>
              {previewSrc ? (
                <img
                  key={page}
                  src={previewSrc}
                  alt={`${manifest.filename} page ${page}`}
                  style={{ maxWidth: "100%", border: "1px solid var(--c-border, #ccc)" }}
                />
              ) : (
                <p className="dash__intro">Loading page {page}…</p>
              )}
            </>
          )}

          {Object.keys(detail.headerMeta).length > 0 ? (
            <>
              <h4>Document details</h4>
              <dl>
                {Object.entries(detail.headerMeta).map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
            </>
          ) : null}

          {detail.notes.length > 0 ? (
            <>
              <h4>What the parser found</h4>
              {/* The DELTA arithmetic cross-check lands here. It is the EVIDENCE for the
                  pre-selected quantity column — showing its working is what makes the default a
                  reasoned proposal rather than a guess the human rubber-stamps. */}
              <ul aria-label="Parser notes">
                {detail.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            </>
          ) : null}
        </section>

        {/* ── RIGHT: the mapping, the grid, and the commit ────────────────────────────── */}
        <div style={{ minWidth: 0, display: "grid", gap: "1rem" }}>
          <section className="card dash-section" aria-label="Column mapping">
            <h4>Columns</h4>
            <p className="dash__intro">
              The importer proposed these. Change any that are wrong — only mapped columns are
              imported.
            </p>
            <div style={{ overflowX: "auto" }}>
              <table className="dash-table">
                <thead>
                  <tr>
                    <th scope="col">Column</th>
                    <th scope="col">Imported as</th>
                  </tr>
                </thead>
                <tbody>
                  {Array.from({ length: width }, (_, i) => i).map((i) => {
                    const label =
                      detail.columnMap?.labels?.[String(i)] ?? headerCells[i] ?? `Column ${i + 1}`;
                    return (
                      <tr key={i}>
                        <td>{label || `Column ${i + 1}`}</td>
                        <td>
                          <select
                            aria-label={`Import column ${label || i + 1} as`}
                            value={concepts[i] ?? "ignore"}
                            onChange={(e) => {
                              const v = e.target.value;
                              setConcepts((c) => {
                                const next = { ...c };
                                // A concept can only claim ONE column — clear any previous holder
                                // so two columns can never both be "Description".
                                if (v !== "ignore") {
                                  for (const k of Object.keys(next)) {
                                    if (next[Number(k)] === v) delete next[Number(k)];
                                  }
                                  next[i] = v;
                                } else {
                                  delete next[i];
                                }
                                return next;
                              });
                              setPlan(null);
                            }}
                          >
                            {CONCEPT_ORDER.map((c) => (
                              <option key={c} value={c}>
                                {CONCEPT_LABELS[c]}
                              </option>
                            ))}
                          </select>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {(detail.columnMap?.qty_candidates?.length ?? 0) > 1 ? (
              <>
                <h4>Which column is the quantity?</h4>
                <p className="dash__intro">
                  This document has more than one quantity column. The importer pre-selected the
                  one its arithmetic check agrees with — confirm it.
                </p>
                <select
                  aria-label="Quantity column"
                  value={qtyCol ?? ""}
                  onChange={(e) => {
                    setQtyCol(e.target.value === "" ? null : Number(e.target.value));
                    setPlan(null);
                  }}
                >
                  <option value="">— choose —</option>
                  {(detail.columnMap?.qty_candidates ?? []).map((i) => (
                    <option key={i} value={i}>
                      {detail.columnMap?.labels?.[String(i)] ?? headerCells[i] ?? `Column ${i + 1}`}
                    </option>
                  ))}
                </select>
              </>
            ) : null}
          </section>

          <section className="card dash-section" aria-label="Manifest rows">
            <h4>
              Rows ({resolvedLines.length} of {importableRows.length + added.length} selected)
            </h4>
            {missingDescription > 0 ? (
              <p className="dash-error">
                {missingDescription} selected line
                {missingDescription === 1 ? " has" : "s have"} no description — the importer will
                refuse them. Map a Description column, or edit those rows.
              </p>
            ) : null}
            <div style={{ overflowX: "auto", maxHeight: "28rem" }}>
              <table className="dash-table">
                <thead>
                  <tr>
                    <th scope="col">Import</th>
                    <th scope="col">#</th>
                    {Array.from({ length: width }, (_, i) => (
                      <th key={i} scope="col">
                        {headerCells[i] || `Column ${i + 1}`}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {importableRows.map((r) => {
                    const cells = parseCells(r.cells_json);
                    const st = rowState[r.row_index] ?? { keep: false, edits: {} };
                    const flags = (r.flags ?? "").split(",").filter(Boolean);
                    return (
                      <tr key={r.row_index}>
                        <td>
                          <input
                            type="checkbox"
                            checked={st.keep}
                            aria-label={`Import source row ${r.row_index}`}
                            onChange={(e) => setKeep(r.row_index, e.target.checked)}
                          />
                        </td>
                        <td>
                          {r.row_index}
                          {flags.length > 0 ? (
                            <span title={flags.map((f) => FLAG_COPY[f] ?? f).join("; ")}> ⚠</span>
                          ) : null}
                        </td>
                        {Array.from({ length: width }, (_, i) => (
                          <td key={i}>
                            <input
                              type="text"
                              value={st.edits[i] ?? cells[i] ?? ""}
                              aria-label={`Row ${r.row_index} column ${i + 1}`}
                              onChange={(e) => setEdit(r.row_index, i, e.target.value)}
                            />
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section className="card dash-section" aria-label="Add a missing line">
            <h4>Add a line the document missed</h4>
            {added.map((a, i) => (
              <div className="dash-row" key={a.key}>
                <input
                  type="text"
                  placeholder="Description"
                  aria-label={`Added line ${i + 1} description`}
                  value={a.description}
                  onChange={(e) =>
                    setAdded((rowsA) =>
                      rowsA.map((x) => (x.key === a.key ? { ...x, description: e.target.value } : x)),
                    )
                  }
                />
                <input
                  type="text"
                  placeholder="Part number"
                  aria-label={`Added line ${i + 1} part number`}
                  value={a.part_number}
                  onChange={(e) =>
                    setAdded((rowsA) =>
                      rowsA.map((x) => (x.key === a.key ? { ...x, part_number: e.target.value } : x)),
                    )
                  }
                />
                <input
                  type="text"
                  placeholder="Qty"
                  aria-label={`Added line ${i + 1} quantity`}
                  value={a.qty}
                  onChange={(e) =>
                    setAdded((rowsA) =>
                      rowsA.map((x) => (x.key === a.key ? { ...x, qty: e.target.value } : x)),
                    )
                  }
                />
                <input
                  type="text"
                  placeholder="Unit"
                  aria-label={`Added line ${i + 1} unit`}
                  value={a.unit}
                  onChange={(e) =>
                    setAdded((rowsA) =>
                      rowsA.map((x) => (x.key === a.key ? { ...x, unit: e.target.value } : x)),
                    )
                  }
                />
                <button
                  type="button"
                  className="btn btn--secondary"
                  aria-label={`Remove added line ${i + 1}`}
                  onClick={() => {
                    setAdded((rowsA) => rowsA.filter((x) => x.key !== a.key));
                    setPlan(null);
                  }}
                >
                  Remove
                </button>
              </div>
            ))}
            <button
              type="button"
              className="btn btn--secondary"
              onClick={() => {
                setAdded((rowsA) => [
                  ...rowsA,
                  {
                    key: `added-${rowsA.length}-${importableRows.length}`,
                    description: "",
                    part_number: "",
                    qty: "",
                    unit: "",
                  },
                ]);
                setPlan(null);
              }}
            >
              + Add a line
            </button>
          </section>

          <section className="card dash-section" aria-label="Preview and import">
            <h4>Import</h4>
            <div className="dash-row">
              <label htmlFor="manifest-mode">If a part number is already on the list</label>
              <select
                id="manifest-mode"
                aria-label="Import mode"
                value={mode}
                onChange={(e) => setMode(e.target.value as api.ManifestMode)}
              >
                <option value="add_new">Add every line as new</option>
                <option value="merge">Merge onto the matching line</option>
              </select>
            </div>

            <button
              type="button"
              className="btn btn--secondary"
              disabled={busy !== null || !committable || resolvedLines.length === 0}
              onClick={() => void runPlan()}
            >
              {busy === "plan" ? "Checking…" : "Preview changes"}
            </button>

            {plan ? (
              <div aria-label="Preview result">
                <p>
                  {plan.counts.incoming} line{plan.counts.incoming === 1 ? "" : "s"} →{" "}
                  <strong>{plan.counts.new} new</strong>, {plan.counts.matched} already on the list,{" "}
                  {plan.counts.ambiguous} ambiguous, {plan.counts.absent} on the list but not in
                  this document.
                </p>
                {plan.would_exceed_line_cap ? (
                  <p className="dash-error">
                    This job would hold {plan.projected_total} lines, over the 500 the materials
                    page can show. Split the import or trim the list.
                  </p>
                ) : null}

                {plan.ambiguous.length > 0 ? (
                  <>
                    <h4>These part numbers match more than one existing line</h4>
                    <p className="dash__intro">
                      Duplicate part numbers are normal in a BOM. Pick which line each one belongs
                      to — the import will not run until every one is decided.
                    </p>
                    <div style={{ overflowX: "auto" }}>
                      <table className="dash-table">
                        <thead>
                          <tr>
                            <th scope="col">Row</th>
                            <th scope="col">Part number</th>
                            <th scope="col">Which line?</th>
                          </tr>
                        </thead>
                        <tbody>
                          {plan.ambiguous.map((a) => (
                            <tr key={a.source_row_index}>
                              <td>{a.source_row_index}</td>
                              <td>{a.part_number}</td>
                              <td>
                                <select
                                  aria-label={`Which existing line for row ${a.source_row_index}`}
                                  value={ambiguousChoice[a.source_row_index] ?? ""}
                                  onChange={(e) =>
                                    setAmbiguousChoice((c) => ({
                                      ...c,
                                      [a.source_row_index]: Number(e.target.value),
                                    }))
                                  }
                                >
                                  <option value="">— choose —</option>
                                  {a.line_ids.map((id) => (
                                    <option key={id} value={id}>
                                      Existing line #{id}
                                    </option>
                                  ))}
                                </select>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                ) : null}

                {plan.absent.length > 0 ? (
                  <p className="dash__intro">
                    {plan.absent.length} line{plan.absent.length === 1 ? "" : "s"} already on this
                    job {plan.absent.length === 1 ? "is" : "are"} not in this document. Nothing is
                    removed — a partial BOM is not a deletion.
                  </p>
                ) : null}
              </div>
            ) : (
              <p className="dash__intro">
                Preview first — it checks these lines against the job without changing anything.
              </p>
            )}

            {progress ? <p role="status">Importing {progress}</p> : null}

            <div className="dash-row">
              <button
                type="button"
                className="btn btn--primary"
                disabled={
                  busy !== null ||
                  !committable ||
                  plan === null ||
                  unresolvedAmbiguous.length > 0 ||
                  missingDescription > 0 ||
                  resolvedLines.length === 0
                }
                onClick={() => void runCommit()}
              >
                {busy === "commit" ? "Importing…" : `Import ${resolvedLines.length} lines`}
              </button>
              <button
                type="button"
                className="btn btn--secondary"
                disabled={busy !== null}
                onClick={() => void runDiscard()}
              >
                Discard this manifest
              </button>
            </div>
            {plan && unresolvedAmbiguous.length > 0 ? (
              <p className="dash-error">
                {unresolvedAmbiguous.length} ambiguous part number
                {unresolvedAmbiguous.length === 1 ? "" : "s"} still to decide.
              </p>
            ) : null}
          </section>
        </div>
      </div>
      ) : null}
    </>
  );
}
