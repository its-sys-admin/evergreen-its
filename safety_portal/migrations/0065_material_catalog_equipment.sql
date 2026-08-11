-- 0065 — the EQUIPMENT found in the project BOM corpus (operator ask, 2026-08-11).
--
-- Ten bills of materials from three racking vendors (Nevados, Terrasmart/TerraTrak, GameChange
-- Solar) were reconciled against `material_catalog`. 218 unique parts; the overlap with the
-- existing 37 rows was ZERO — the two describe different layers. The catalogue holds
-- DATASHEET-LEVEL EQUIPMENT (modules, inverters, transformers, combiners); the BOMs are
-- overwhelmingly installation-level components (hex flange nuts, torque tubes, driven piles).
--
-- SCOPE (operator decision): add the EQUIPMENT only. The 28 rows below are the actuators,
-- drive motors, SCADA/monitoring devices, cable and bonding hardware that carry a real
-- manufacturer spec. The ~190 fasteners, tubes, posts, brackets and mounting kits are
-- DELIBERATELY EXCLUDED — adding them would take the catalogue past 250 rows and turn the
-- field expected-materials picker into a list nobody can scroll on a phone. A structural part
-- can always be added later, one row at a time, through the admin surface.
--
-- model_id: the manufacturer's model where the BOM description carries one (PA 16-Y0008-001,
-- WRSS915, SR50AT-L15-PT, RAD06, WEEB-DSK14, ST-150R-12CID2, HE9C-61MHD-12003RC-DA216);
-- otherwise the VENDOR PART NUMBER, which is itself a stable identifier the office already
-- orders against. Every row's key_specs ends with `vendor P/N <n>` so the BOM line is always
-- traceable from the catalogue entry, whichever form the model_id took.
--
-- MANUFACTURER — read this column as "who Evergreen gets it from", which for most of these
-- rows is genuinely also the maker: the Nevados actuators/QP/QCC/wireless controls, the
-- TerraTrak station and row-control assemblies, and the GameChange SDC/motor-control
-- assemblies are all vendor-branded system components. Two caveats, recorded rather than
-- papered over: (1) where the BOM description names a different maker it wins — the snow
-- sensor is Campbell Scientific, not Nevados; (2) a small number of items are third-party
-- parts merely RESOLD under the racking vendor's part number — WEEB-DSK14 is the clear case
-- (a Wiley bonding washer carried as Terrasmart P/N 119940). It is recorded against the
-- supplier because that is what the source document actually supports and what the office
-- orders against; correcting it to the true maker is an operator edit, not a guess to make
-- here.
--
-- NOT ADDED, deliberately — the Bonacci BOMs name their PV module as `VSUN###N-132BMHR-DG`
-- at 615 W. The `###` is a literal placeholder in the source PDF, not a redaction by this
-- migration: the vendor shipped a templated model number. Inventing the missing digits would
-- put a fabricated part number in the system of record, so the module is omitted and the
-- operator supplies the real model.
--
-- THREE NEW CATEGORIES: `actuator`, `scada`, `cable`. `category` is free-text (length-checked
-- only — see fieldops_material_write.ts), lowercase by the convention the original seed set.
-- The pre-existing `Racking` and `racking motor` rows break that convention; they are left
-- alone here because retiring/renaming live rows is a separate operator decision.
--
-- IDEMPOTENT: each INSERT is guarded on model_id, so a re-run (or a row an admin already
-- created by hand) is a no-op rather than a duplicate. `material_catalog` has no unique index
-- on model_id, so the guard is the only thing preventing doubles.
--
-- unit_cost is left NULL — the authoritative money lives on the per-job Material List line,
-- never here (the 0019 rule, unchanged).
--
-- Writers: fieldops_material_write.ts (admin CRUD). Readers: GET /api/fieldops/materials,
-- the Materials Catalogue page, and the per-job expected-materials picker.
--
-- DEPLOY ORDER: no schema change — columns are unchanged from 0019, so this migration is
-- data-only and safe to apply before or after a Worker deploy.

-- SQLite names VALUES columns `column1..N` — it does NOT support the `AS v(a, b, c)`
-- column-alias form (that is PostgreSQL). Referencing them positionally is the portable way.
INSERT INTO material_catalog (model_id, manufacturer, category, key_specs, source_files)
SELECT column1, column2, column3, column4, column5 FROM (VALUES
  ('PA 16-Y0008-001', 'Nevados', 'actuator', 'Linear actuator, 65 kN · vendor P/N 7000727', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf"]'),
  ('7006955', 'Nevados', 'actuator', 'Actuator, 18 mm end rod, 240 VAC, 65 kN, 410 mm · vendor P/N 7006955', '["ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('HE9C-61MHD-12003RC-DA216', 'Terrasmart', 'racking motor', 'Slew gear & motor assembly · vendor P/N 805001', '["25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf", "25-35120 - EVERGREEN ENERGY - BONACCI 2 - DELTA BOM (1).pdf", "DeepLakeShippingLog.xlsx", "KiwiLog.xlsx"]'),
  ('ST-150R-12CID2', 'Terrasmart', 'module', 'Mono-crystalline module · vendor P/N 805000', '["25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf", "25-35120 - EVERGREEN ENERGY - BONACCI 2 - DELTA BOM (1).pdf", "DeepLakeShippingLog.xlsx", "KiwiLog.xlsx"]'),
  ('7000083', 'Nevados', 'scada', 'Anemometer, RS485 connection · vendor P/N 7000083', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('SR50AT-L15-PT', 'Campbell Scientific', 'scada', 'Snow sensor, tinned leads · vendor P/N 7000118', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('RAD06', 'Nevados', 'scada', '6-plate solar radiation shield · vendor P/N 7000237', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7000248', 'Nevados', 'scada', 'Anemoscope (wind direction) · vendor P/N 7000248', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7000930', 'Nevados', 'scada', 'QP monitoring unit · vendor P/N 7000930', '["Bradley 1 Customer BOM.pdf"]'),
  ('WRSS915', 'Nevados', 'scada', 'Wireless master, 915 MHz · vendor P/N 7000932', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7001114', 'Nevados', 'scada', 'Irradiation sensor · vendor P/N 7001114', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7001248', 'Nevados', 'scada', 'Antenna, 915 MHz, N-type connector · vendor P/N 7001248', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7002284', 'Nevados', 'scada', 'QCC controller, outdoor installation · vendor P/N 7002284', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7005574', 'Nevados', 'scada', 'GPS receiver · vendor P/N 7005574', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7005840', 'Nevados', 'scada', 'Control unit, SKC, UL listed · vendor P/N 7005840', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('20436-000', 'GameChange Solar', 'scada', 'GPS assembly · vendor P/N 20436-000', '["CommunityPowerGroup_Steger_MasterBOM_09022025.pdf"]'),
  ('SDC V3-R1', 'GameChange Solar', 'scada', 'Solar drive controller, IEC, RR · vendor P/N 21086-100', '["CommunityPowerGroup_Steger_MasterBOM_09022025.pdf"]'),
  ('21079-000', 'GameChange Solar', 'scada', '6x motor control assembly, 100-240 V · vendor P/N 21079-000', '["CommunityPowerGroup_Steger_MasterBOM_09022025.pdf"]'),
  ('802569', 'Terrasmart', 'scada', 'Row controls assembly, dual input · vendor P/N 802569', '["25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf", "25-35120 - EVERGREEN ENERGY - BONACCI 2 - DELTA BOM (1).pdf", "DeepLakeShippingLog.xlsx", "KiwiLog.xlsx"]'),
  ('802647', 'Terrasmart', 'scada', 'TerraTrak repeater kit · vendor P/N 802647', '["DeepLakeShippingLog.xlsx"]'),
  ('805226', 'Terrasmart', 'scada', 'Weather station assembly, Solarland pony panel · vendor P/N 805226', '["25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf", "25-35120 - EVERGREEN ENERGY - BONACCI 2 - DELTA BOM (1).pdf", "DeepLakeShippingLog.xlsx", "KiwiLog.xlsx"]'),
  ('805227', 'Terrasmart', 'scada', 'Network station assembly, Solarland pony panel · vendor P/N 805227', '["25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf", "25-35120 - EVERGREEN ENERGY - BONACCI 2 - DELTA BOM (1).pdf", "DeepLakeShippingLog.xlsx", "KiwiLog.xlsx"]'),
  ('802002', 'Terrasmart', 'scada', 'TerraTrak AC power unit · vendor P/N 802002', '["25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf", "25-35120 - EVERGREEN ENERGY - BONACCI 2 - DELTA BOM (1).pdf", "DeepLakeShippingLog.xlsx", "KiwiLog.xlsx"]'),
  ('7000120', 'Nevados', 'cable', 'Actuator cable, 4x1.5 mm2 + 3x0.5 mm2 · vendor P/N 7000120', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7000161', 'Nevados', 'cable', 'SKC cable, outdoor rated, 600 VAC, 3x6 mm2 · vendor P/N 7000161', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('7000840', 'Nevados', 'cable', 'Coaxial cable, N-type male to SMA male, 50 ohm · vendor P/N 7000840', '["Bradley 1 Customer BOM.pdf", "Bradley 2 Customer BOM.pdf", "Brimfield 1 Customer BOM.pdf", "Brimfield 2 Customer BOM.pdf", "ESS-CPG Roxbury - Customer BOM - 10-23-25.xlsx"]'),
  ('50001-000', 'GameChange Solar', 'cable', 'Wire, 6 cond 18 AWG, direct burial, 600 V · vendor P/N 50001-000', '["CommunityPowerGroup_Steger_MasterBOM_09022025.pdf"]'),
  ('WEEB-DSK14', 'Terrasmart', 'grounding', 'Bonding washer, WEEB DSK14 · vendor P/N 119940', '["25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf", "25-35120 - EVERGREEN ENERGY - BONACCI 2 - DELTA BOM (1).pdf", "KiwiLog.xlsx"]')
)
WHERE NOT EXISTS (
  SELECT 1 FROM material_catalog mc WHERE mc.model_id = column1
);
