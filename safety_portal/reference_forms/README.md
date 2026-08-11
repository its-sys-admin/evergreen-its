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

## OSHA toolbox-talk sources (added 2026-08-11)

Operator-requested expansion of the Toolbox Talk parent from 5 topic variants to 34.
Each row below is **one source document → one `content_signin` form definition** — a
toolbox talk is a single ~5-minute talk with its own signed training record, so the
granularity is deliberately one-form-per-document, not one-form-per-subject-area.

The 27 `OSHA_TBT_*` files were downloaded from **oshatraining.com**'s free toolbox-talk
library (OSHA Training Services Inc.). They are redistributed here unaltered as the
transcription source, and every definition carries that document's **own** attribution
footer verbatim — the copyright year and wording genuinely differ across the corpus
(2014 / 2018 / 2023 / 2025), so the footer is per-file, never a template.

The last two are **U.S. Government works (public domain)**: an OSHA publication and an
archival capture of the official eCFR regulation text.

| Committed file | Form definition | Source |
|---|---|---|
| `OSHA_TBT_Fire_Extinguishers_Proper_Selection.pdf` | `toolbox-talk-fire-ext-selection-v1` | oshatraining.com |
| `OSHA_TBT_Fire_Extinguishers_How_They_Extinguish_Fires.pdf` | `toolbox-talk-fire-ext-how-they-work-v1` | oshatraining.com |
| `OSHA_TBT_Fire_Extinguishers_Remember_PASS.pdf` | `toolbox-talk-fire-ext-pass-v1` | oshatraining.com |
| `OSHA_TBT_Fire_Extinguishers_Dos_and_Donts.pdf` | `toolbox-talk-fire-ext-dos-donts-v1` | oshatraining.com |
| `OSHA_TBT_Fire_Extinguishers_Making_Sure_They_Are_Ready.pdf` | `toolbox-talk-fire-ext-readiness-v1` | oshatraining.com |
| `OSHA_TBT_Fire_Prevention_Flammable_Liquid_Categories.pdf` | `toolbox-talk-flammable-liquid-categories-v1` | oshatraining.com |
| `OSHA_TBT_Fire_Prevention_Avoiding_Accidental_Fires.pdf` | `toolbox-talk-fire-prevention-tips-v1` | oshatraining.com |
| `OSHA_TBT_Cold_Stress_Wind_Chill.pdf` | `toolbox-talk-cold-wind-chill-v1` | oshatraining.com |
| `OSHA_TBT_Cold_Stress_Dressing_for_Cold_Weather.pdf` | `toolbox-talk-cold-dressing-v1` | oshatraining.com |
| `OSHA_TBT_Heat_Illness_What_Is_Heat_Illness.pdf` | `toolbox-talk-heat-what-is-v1` | oshatraining.com |
| `OSHA_TBT_Heat_Illness_Body_Defends_Against_Overheating.pdf` | `toolbox-talk-heat-body-defends-v1` | oshatraining.com |
| `OSHA_TBT_Heat_Illness_Why_Humidity_Makes_Heat_Dangerous.pdf` | `toolbox-talk-heat-humidity-v1` | oshatraining.com |
| `OSHA_TBT_Heat_Illness_Risk_Factors.pdf` | `toolbox-talk-heat-risk-factors-v1` | oshatraining.com |
| `OSHA_TBT_Heat_Illness_Recognizing_and_Responding.pdf` | `toolbox-talk-heat-recognize-respond-v1` | oshatraining.com |
| `OSHA_TBT_Heat_Illness_Hydration.pdf` | `toolbox-talk-heat-hydration-v1` | oshatraining.com |
| `OSHA_TBT_Heat_Illness_Rest_and_Shade.pdf` | `toolbox-talk-heat-rest-shade-v1` | oshatraining.com |
| `OSHA_TBT_Heat_Illness_Clothing_and_PPE.pdf` | `toolbox-talk-heat-clothing-ppe-v1` | oshatraining.com |
| `OSHA_TBT_Mental_Health_Suicide_Prevention_Construction.pdf` | `toolbox-talk-mental-health-construction-v1` | oshatraining.com |
| `OSHA_TBT_Excavation_Standards_Key_Definitions.pdf` | `toolbox-talk-excavation-standards-v1` | oshatraining.com |
| `OSHA_TBT_Excavation_Crossing_Over_Excavations.pdf` | `toolbox-talk-excavation-crossing-v1` | oshatraining.com |
| `OSHA_TBT_Excavation_Signs_of_Distressed_Soil.pdf` | `toolbox-talk-excavation-distressed-soil-v1` | oshatraining.com |
| `OSHA_TBT_Excavation_Ladders_In_and_Out.pdf` | `toolbox-talk-excavation-ladders-v1` | oshatraining.com |
| `OSHA_TBT_Material_Handling_Forklifts_and_PITs.pdf` | `toolbox-talk-forklifts-pits-v1` | oshatraining.com |
| `OSHA_TBT_Material_Handling_Palletized_Materials.pdf` | `toolbox-talk-palletized-materials-v1` | oshatraining.com |
| `OSHA_TBT_Material_Handling_Safe_Lifting_Techniques.pdf` | `toolbox-talk-safe-lifting-v1` | oshatraining.com |
| `OSHA_TBT_Material_Handling_Mobile_Cranes.pdf` | `toolbox-talk-mobile-cranes-v1` | oshatraining.com |
| `OSHA_TBT_Material_Handling_Storage_Rack_Safety.pdf` | `toolbox-talk-storage-racks-v1` | oshatraining.com |
| `OSHA_Severe_Weather_Safety_Awareness_Poster.pdf` | `toolbox-talk-severe-weather-v1` | OSHA (DTSEM 09/2025), public domain |
| `OSHA_Housekeeping_29_CFR_1926_25_and_1910_22.pdf` | `toolbox-talk-housekeeping-v1` | eCFR 29 CFR §1926.25 + §1910.22, public domain |

**Severe Weather and Housekeeping have no oshatraining.com talk** — all 16 categories /
182 documents on that site were enumerated and neither topic exists there, so both were
built from the official OSHA / eCFR record instead. The severe-weather poster covers
tornadoes, high winds, lightning and flooding; it does **not** address hurricanes by name
(see `docs/tech_debt.md`).

Fidelity is mechanically enforced, not asserted: every sentence in a definition must appear
in its source text and every substantive source line must appear in the definition, compared
on a punctuation- and spacing-insensitive reduction so a faithful transcription's legitimate
differences (ligatures, curly quotes, line-wrap hyphenation) do not register as drift.
