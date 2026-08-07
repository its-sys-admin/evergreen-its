import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";

// ROADMAP Track 6 — migration 0058 (job archive state + cap.job.archive).
//
// A migration is the one artifact whose correctness cannot be inferred from the code that reads
// it: the columns either exist with the right defaults on a real D1 or they do not. These assert
// the shape everything downstream assumes, and — more importantly — the two decisions that are
// easy to get silently wrong (the grant's SCOPE, and the fail-safe default of archive_state).

describe("migration 0058 — job archive state", () => {
  it("adds the seven archive columns with archive-workflow-neutral defaults", async () => {
    const cols = await env.DB.prepare(
      "SELECT name, `notnull`, dflt_value FROM pragma_table_info('jobs') WHERE name LIKE 'archive%' ORDER BY name",
    ).all<{ name: string; notnull: number; dflt_value: string | null }>();

    expect(cols.results.map((c) => c.name)).toEqual([
      "archive_attempts",
      "archive_completed_at",
      "archive_detail",
      "archive_direction",
      "archive_folder_key",
      "archive_requested_at",
      "archive_state",
    ]);

    const byName = Object.fromEntries(cols.results.map((c) => [c.name, c]));
    // The four NOT NULL columns carry defaults, so the ALTERs backfill every existing row rather
    // than failing on a non-empty table.
    expect(byName["archive_state"].notnull).toBe(1);
    expect(byName["archive_direction"].notnull).toBe(1);
    expect(byName["archive_attempts"].notnull).toBe(1);
    expect(byName["archive_detail"].notnull).toBe(1);
    // The two timestamps are deliberately nullable — "never requested" is not epoch 0.
    expect(byName["archive_requested_at"].notnull).toBe(0);
    expect(byName["archive_completed_at"].notnull).toBe(0);
  });

  it("backfills every pre-existing job as 'never entered the archive workflow'", async () => {
    // True of every job in the system today: the §51 move has never once fired live.
    const row = await env.DB.prepare(
      "SELECT count(*) AS n FROM jobs WHERE archive_state != 'none' OR archive_direction != '' " +
        "OR archive_attempts != 0 OR archive_requested_at IS NOT NULL OR archive_completed_at IS NOT NULL",
    ).first<{ n: number }>();
    expect(row!.n).toBe(0);
  });

  it("grants cap.job.archive to admin ONLY", async () => {
    // 0013's admin grant was a seed-time catch-all over the capabilities table and does NOT
    // auto-include anything added later — so this grant has to be explicit, and it has to be
    // narrow. Archiving is a heavyweight cross-system relocation; a manager holding it by
    // accident is a real privilege widening, not a cosmetic one.
    const cap = await env.DB.prepare(
      "SELECT key FROM capabilities WHERE key = 'cap.job.archive'",
    ).first<{ key: string }>();
    expect(cap?.key).toBe("cap.job.archive");

    const grants = await env.DB.prepare(
      "SELECT role_key FROM role_capabilities WHERE capability_key = 'cap.job.archive' ORDER BY role_key",
    ).all<{ role_key: string }>();
    expect(grants.results.map((g) => g.role_key)).toEqual(["admin"]);
  });

  it("keeps cap.job.archive DISTINCT from cap.jobtracker.manage", async () => {
    // Deliberately separate so archiving can be narrowed later without also revoking routine
    // job create / rename / close. If these ever collapse into one key, that decision is gone.
    const both = await env.DB.prepare(
      "SELECT count(*) AS n FROM capabilities WHERE key IN ('cap.job.archive','cap.jobtracker.manage')",
    ).first<{ n: number }>();
    expect(both!.n).toBe(2);
  });

  it("is re-appliable — the grant inserts are INSERT OR IGNORE", async () => {
    // Migrations can be re-run against a partially-migrated D1; a duplicate-key throw here would
    // wedge the whole chain.
    await env.DB.prepare(
      "INSERT OR IGNORE INTO capabilities (key, label, description) VALUES ('cap.job.archive','x','y')",
    ).run();
    await env.DB.prepare(
      "INSERT OR IGNORE INTO role_capabilities (role_key, capability_key) VALUES ('admin','cap.job.archive')",
    ).run();
    const n = await env.DB.prepare(
      "SELECT count(*) AS n FROM role_capabilities WHERE capability_key = 'cap.job.archive'",
    ).first<{ n: number }>();
    expect(n!.n).toBe(1);
  });
});
