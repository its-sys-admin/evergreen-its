-- WPR photo pipeline: origin tagging + pool captions + screened thumbnails (Track B, 2026-08-13).
--
-- THREE additive nullable-or-defaulted columns on daily_photo_pool (0037); no rewrite, no index
-- change. What each is for:
--
--   origin — which door the row came through, closed vocabulary:
--     'field'       (DEFAULT) the crew's Additional-site-photos upload (the 0037 flow, unchanged);
--     'office_wpr'  an office upload from the weekly-report screen (same route, capability-gated
--                   extra vocab) — prune retains these 90d unclaimed instead of 7d, because a
--                   weekly-cadence document outlives the field flow's abandonment window;
--     'site_photos' a BRIDGE registration: the Mac's intake screens the daily report's INLINE
--                   site_photos (§34) and files them to Box exactly as before, then registers each
--                   clean one here (POST /api/internal/daily-photos/register) so the WPR photo
--                   picker — which reads ONLY this pool — can finally see them. Bridge rows are
--                   born clean + CLAIMED by their submission (prune-immune like any claimed
--                   manifest) and carry NO bytes (photo_json NULL, hmac sentinel 'registered:v1' —
--                   nothing reads hmac off non-pending rows; the pending screening queue serves
--                   status='pending' only, which a bridge row never is).
--
--   caption — the intake-built photo caption (photo_screen.build_caption: name + EXIF taken_at/GPS
--     sidecar) for bridge rows; the additional_photos flow keeps captions inside the submission
--     payload refs, so this column stays NULL there and the WPR query COALESCEs pool-first.
--
--   thumb_b64 — a SMALL (≤40,000 bytes decoded; ~320px JPEG q70) thumbnail derived from the §34
--     CLEAN RE-ENCODE (never the raw upload), posted by the Mac with the screening result (or the
--     bridge registration). This DELIBERATELY RELAXES the 0037 Option-D "record-only, no serving
--     route, ever" posture — for thumbnails only, operator-approved 2026-08-13: the office was
--     picking report photos blind by date+caption, which is exactly the curation the WPR needs
--     eyes for. Originals still NEVER ride D1 past screening and are NEVER served; the one new
--     serving route (GET /api/fieldops/daily-photo/:id/thumb, session + cap.jobtracker.manage —
--     the WPR screen's own gate) serves clean rows' thumbs only.
--
-- APPLY BEFORE DEPLOY: run `npx wrangler d1 migrations apply its-safety-portal-db --remote`
-- BEFORE the Worker that reads/writes these columns deploys — else the register/result/report
-- routes 500. Same rule as 0010/0033/0036/0037. (Always `git pull` ~/its to latest main FIRST —
-- the stale-migrations-list lockout class, forensic #2.)

ALTER TABLE daily_photo_pool ADD COLUMN origin    TEXT NOT NULL DEFAULT 'field';
ALTER TABLE daily_photo_pool ADD COLUMN caption   TEXT;
ALTER TABLE daily_photo_pool ADD COLUMN thumb_b64 TEXT;
