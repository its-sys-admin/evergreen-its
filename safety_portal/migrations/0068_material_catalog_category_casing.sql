-- 0068 — normalise the two hand-added material_catalog categories (operator ask, 2026-08-11).
--
-- `material_catalog.category` is FREE TEXT — fieldops_material_write.ts only length-checks it
-- (`invalid_category` on empty or over-long), so any string an admin types becomes a category
-- and a new bucket on the Materials Catalogue page. Two rows drifted off the lowercase
-- convention the 0019 seed established and every other category still follows:
--
--   'Racking'        — capitalised. One row: SPR-M-STRESS-1b by manufacturer "Acme2", a
--                      leftover stress-test artifact, already retired (active = 0).
--   'racking motor'  — space-separated where the convention is snake_case (cf. 'breaker_box').
--                      Two rows: D001-PD16002 (seeded by 0019 as 'other', re-categorised by
--                      hand at runtime) and HE9C-61MHD-12003RC-DA216 (added by 0065, which
--                      deliberately matched the category already in live use).
--
-- Left as-is on purpose: the Acme2 row itself. It is test data, but it is already soft-retired
-- and `material_catalog` never hard-deletes — receipts and incidents reference catalog_id, so a
-- deleted row would strand them (the 0019 rule). Its category is normalised here so no
-- capitalised bucket exists in the data at all; the row stays invisible either way.
--
-- Why a migration rather than an admin edit: this is a rename of LIVE rows, and doing it here
-- keeps the change reviewable, versioned, and identical across environments. On a freshly stood
-- up tenant both UPDATEs are no-ops — neither category exists in the 0019 seed — which is the
-- intended behaviour, not an accident.
--
-- Idempotent: re-running matches nothing the second time. Data-only, no schema change, so it is
-- safe to apply before or after a Worker deploy.

UPDATE material_catalog SET category = 'racking_motor', updated_at = unixepoch()
WHERE category = 'racking motor';

UPDATE material_catalog SET category = 'racking', updated_at = unixepoch()
WHERE category = 'Racking';
