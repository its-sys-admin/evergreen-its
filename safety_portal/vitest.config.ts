import { defineConfig } from "vitest/config";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-pool-workers";
import path from "node:path";

// Worker tests run in workerd (the real runtime) with a Miniflare D1 — NOT mocks.
// This is the antidote to the "SimpleNamespace mocks pass, live API rejects" class:
// requireRole, the submit-as gate, and the last-admin guard are exercised against a
// real D1 with the real migrations applied. See safety_portal/README.md "Testing".
//
// vitest-pool-workers 0.16 (vitest 4 line): cloudflareTest() is a Vite *plugin*
// (the older defineWorkersConfig/`/config` subpath was removed). It reads bindings
// from wrangler.jsonc; secrets (absent there) + TEST_MIGRATIONS are supplied below.
export default defineConfig(async () => {
  const migrations = await readD1Migrations(path.join(import.meta.dirname, "migrations"));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            SESSION_SIGNING_SECRET: "test-session-signing-secret",
            HMAC_PAYLOAD_SECRET: "test-hmac-payload-secret",
            PORTAL_INTERNAL_API_TOKEN: "test-internal-token",
            PORTAL_ADMIN_API_TOKEN: "test-admin-token",
            PORTAL_FIELDOPS_API_TOKEN: "test-fieldops-token",
            PORTAL_PO_API_TOKEN: "test-po-token",
            PORTAL_ESTIMATE_API_TOKEN: "test-estimate-token",
            PORTAL_RFQ_API_TOKEN: "test-rfq-token",
            PORTAL_MANIFEST_API_TOKEN: "test-manifest-token",
            PORTAL_SCHEDULE_API_TOKEN: "test-schedule-token",
            PORTAL_CONFIG_API_TOKEN: "test-config-token",
            PORTAL_SUB_API_TOKEN: "test-sub-token",
          },
        },
      }),
    ],
    test: {
      setupFiles: ["./test/apply-migrations.ts"],
      // Workers-pool suites live in test/ ONLY. Scope the include so this config never
      // collects the SPA jsdom render-smoke suite under src/ (src/**/*.test.tsx) — that
      // suite cannot load in workerd (no DOM / no jsdom) and runs via `npm run test:spa`
      // (vitest.config.spa.ts). Without this scope the default `**/*.test.*` glob would
      // pull in the SPA test and fail it here. Additive: the existing worker tests all
      // live in test/*.test.ts, so this is the current set, unchanged.
      include: ["test/**/*.test.ts"],
    },
  };
});
