# Reference forms — Phase-4 source-of-truth

The source PDFs the Safety Portal forms and seeded checklist content are modeled on,
committed here as the **canonical reference**. The table below is the ledger — count the
rows rather than trusting a number in this sentence (it said "10" through three separate
additions).

A definition's `source_pdf` must resolve to a file in this directory — a hard CI gate
(`tests/test_form_definitions.py::test_definition_source_pdf_exists`). The converse does
not hold: a PDF may live here purely as the cited source for seeded checklist content
(see the `0061`/`0062` rows).

Source: Evergreen's Box folder `ITS DATA/Safety Sheets/` (operator-maintained). Filenames
below are normalized (spaces → underscores) to match the brief's catalog table; the Box
originals differ only in whitespace.

| Committed file | Maps to catalog | Archetype |
|---|---|---|
| `Daily_JOB_HAZARD_ANALYSIS_template.pdf` | JHA | rows + signatures |
| `Back_Strains_and_Sprains_TBT.pdf` | Toolbox Talk (topic) | content + sign-in |
| `Electrical-Safety_TBT.pdf` | Toolbox Talk (topic) | content + sign-in |
| `Ergonomics-Back-Safety_TBT.pdf` | Toolbox Talk (topic) | content + sign-in |
| `Hard-Hat-Safety_TBT.pdf` | Toolbox Talk (topic) | content + sign-in |
| `PPE_TBT.pdf` | Toolbox Talk (topic) | content + sign-in |
| `blank_forklift-rough-terrain-pre-use-inspection-form.pdf` | Equipment Pre-Inspection (Telehandler) | tri-state checklist |
| `Skid_Steer__Daily_Pre-Inspection_Checklist.pdf` | Equipment Pre-Inspection (Skid Steer) | tri-state checklist |
| `weekly_Safe_Work_Observation_Template.pdf` | HSS&E Work Observation | sectioned assessment |
| `VISITOR-SIGN-IN.pdf` | Visitor Sign-In | rows (not Evergreen-branded — header added in Phase 4) |
| `360_Excavator_Inspection_and_Daily_Checklist.pdf` | Equipment Pre-Inspection (360 Excavator) | **two**-state checklist (Okay/Defective — the source offers no N/A box); source is a 7-day week grid, see the definition's `comment` |
| `GAYK_DOYLE_Piledriver_Daily_and_Weekly_Checklists.pdf` | **page 1** → Equipment Pre-Inspection (GAYK/DOYLE Piledriver); **page 2** → checklist library, migration `0062` | tri-state checklist + inspection-library template |
| `GAYK_DOYLE_Startup_Loading_Securing_Loads.pdf` | *(not a form)* — checklist library, migration `0061` | inspection-library templates |
| `GAYK_NA_Ram_Training_Program_Waiver_and_Checklist.pdf` | Equipment Training Waiver (GAYK/DOYLE Ram) | content + sign-in |

The last four were operator-supplied on 2026-08-10. One of them
(`GAYK_DOYLE_Startup_Loading_Securing_Loads.pdf`) backs **checklist-library templates**, not a form
definition, and another (`GAYK_DOYLE_Piledriver_Daily_and_Weekly_Checklists.pdf`) backs **both** — a
form from page 1 and a library template from page 2.

The three form archetypes Phase 4's `_runtime/` must handle: **rows + signatures**,
**tri-state checklist**, and **sectioned assessment** (plus a per-form `pdf_override.ts`
escape hatch).

## ⚠️ Form-catalog reconciliation (resolve before Phase 4)

The uploaded corpus does **not** match the blueprint's named forms — confirm the real v1
catalog with the operator before seeding `ITS_Forms_Catalog` or building forms:

- **Blueprint** (`mission.md` §8 "Forms catalog at v1") names **four**: JHA, **Daily Site
  Safety Worksheet**, Equipment Pre-Inspection, Toolbox Talk.
- **This corpus** has **no "Daily Site Safety Worksheet"**, and adds two the blueprint does
  not name: **HSS&E Work Observation** (`weekly_Safe_Work_Observation_Template.pdf`) and
  **Visitor Sign-In** (`VISITOR-SIGN-IN.pdf`). Toolbox Talk is represented as five topic
  variants rather than one form.

Neither set is wrong on its face, but they disagree. The catalog scope is an explicit
open item (brief §"Open items / forward") — do not treat this table as the locked v1
catalog without operator sign-off.
