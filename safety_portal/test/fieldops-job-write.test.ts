import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, p as j, g, seedJob as seedJobRow } from "./helpers";
// The SAME §50-edited bundle the Worker serves — asserted served-equals-source, never a pinned
// literal (HOUSE_REFLEXES §5 / po.test.ts pattern), so an operator edit never RED-lights this test.
import deliveryContactsConfig from "../../po_materials/config/delivery_contacts.json";

// ─────────────────────────────────────────────────────────────────────────────
// P2.3 Slice 2 + P2.5 Slice 1/6 — JOB WRITE (create/lifecycle/contacts).
// cap.jobtracker.manage (admin-only). Locks the 0017 origin fence (portal-created jobs are
// origin='portal'/sync_state='pending'), the version vector, the W5 cross-origin scope, and
// Slice 6 — the PORTAL ASSIGNS the canonical JOB-###### from the job_counter (the client no
// longer supplies a job_id; the server returns the assigned one + sets canonical_job_id == job_id).
// ─────────────────────────────────────────────────────────────────────────────


// Slice 6: the portal assigns the Job ID. Create a job and return the SERVER-assigned JOB-######.
// Safety CC is REQUIRED on create (2026-07-24); default one so tests that don't exercise the CC
// itself still create successfully. A test that passes its own safety_cc (incl. FULL) keeps it.
async function createOk(cookie: string, body: Record<string, unknown>): Promise<string> {
  const withCc = "safety_cc" in body ? body : { ...body, safety_cc: ["cc@x.com"] };
  const res = await j(cookie, "/api/fieldops/job", withCc);
  expect(res.status, await res.clone().text()).toBe(201);
  return ((await res.json()) as { job_id: string }).job_id;
}

async function jobRow(jobId: string) {
  return await env.DB.prepare("SELECT * FROM jobs WHERE job_id=?").bind(jobId).first<any>();
}
const seedJob = (jobId: string, status: string): Promise<void> => seedJobRow(jobId, { status, projectName: `P ${jobId}` });
async function audits(action: string) {
  return ((await env.DB.prepare("SELECT * FROM audit_log WHERE action=?").bind(action).all()).results as any[]);
}

let admin: string, submitter: string;
beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM clients"),
    // Reset the Slice-6 allocator to its 0022 seed so each test's first create is JOB-000017.
    // CREATE IF NOT EXISTS + INSERT OR REPLACE self-heal even if a test DROPPED or emptied the
    // table (see the two counter_unavailable cases).
    env.DB.prepare("CREATE TABLE IF NOT EXISTS job_counter (id INTEGER PRIMARY KEY CHECK (id = 1), last_value INTEGER NOT NULL)"),
    env.DB.prepare("INSERT OR REPLACE INTO job_counter (id, last_value) VALUES (1, 16)"),
  ]);
  await provision("admin.one", "password123", "admin");
  await provision("submitter.jim", "password123", "submitter");
  admin = await login("admin.one", "password123");
  submitter = await login("submitter.jim", "password123");
});

describe("POST /api/fieldops/job (create)", () => {
  it("gate: anon → 401, submitter (no manage cap) → 403, admin → 201", async () => {
    expect((await call("/api/fieldops/job", { method: "POST", body: JSON.stringify({ project_name: "X" }) })).status).toBe(401);
    expect((await j(submitter, "/api/fieldops/job", { project_name: "X" })).status).toBe(403);
    expect((await j(admin, "/api/fieldops/job", { project_name: "X", safety_cc: ["cc@x.com"] })).status).toBe(201);
  });

  it("Slice 6: assigns sequential JOB-###### from the counter (seed 16 → first JOB-000017)", async () => {
    const a = await createOk(admin, { project_name: "A" });
    const b = await createOk(admin, { project_name: "B" });
    expect(a).toBe("JOB-000017");
    expect(b).toBe("JOB-000018");
  });

  it("Slice 6: ignores any client-supplied job_id — the portal assigns it", async () => {
    const id = await createOk(admin, { job_id: "CLIENT-CHOSEN", project_name: "X" });
    expect(id).toMatch(/^JOB-\d{6}$/); // server-assigned shape, not the client's string
    expect(await jobRow("CLIENT-CHOSEN")).toBeNull(); // the client's id never reaches D1
  });

  it("stamps the 0017 portal-origin fence + server created_at + canonical=job_id, and audits", async () => {
    const id = await createOk(admin, { project_name: "New Job", progress: 40 });
    expect(id).toMatch(/^JOB-\d{6}$/);
    const row = await jobRow(id);
    expect(row.origin).toBe("portal");
    expect(row.sync_state).toBe("pending");
    expect(row.canonical_job_id).toBe(id); // Slice 6: portal owns the number from birth (not NULL)
    expect(row.active).toBe(1);
    expect(row.status).toBe("active");
    expect(row.progress).toBe(40);
    expect(row.created_at).toBeGreaterThan(1_000_000_000); // server unixepoch(), not the ALTER default 0
    expect(await audits("job_create")).toHaveLength(1);
  });

  it("inline new_client writes a clients row linked to the job", async () => {
    const id = await createOk(admin, { project_name: "C", new_client: { name: "Acme Co", phone: "555" } });
    const row = await jobRow(id);
    const client = await env.DB.prepare("SELECT * FROM clients WHERE id=?").bind(row.client_id).first<any>();
    expect(client.name).toBe("Acme Co");
  });

  it("client_id is verified (422 unknown_client) / linked when valid", async () => {
    expect((await j(admin, "/api/fieldops/job", { project_name: "Z", client_id: 99999, safety_cc: ["cc@x.com"] })).status).toBe(422);
    await env.DB.prepare("INSERT INTO clients (name) VALUES ('Real')").run();
    const cid = (await env.DB.prepare("SELECT id FROM clients WHERE name='Real'").first<{ id: number }>())!.id;
    const id = await createOk(admin, { project_name: "Y", client_id: cid });
    expect((await jobRow(id)).client_id).toBe(cid);
  });

  it("body guard: missing project_name → 400 (no number is burned)", async () => {
    expect((await j(admin, "/api/fieldops/job", { project_name: "" })).status).toBe(400);
    expect((await j(admin, "/api/fieldops/job", {})).status).toBe(400);
    // A rejected create never advances the counter — the first valid create is still JOB-000017.
    expect(await createOk(admin, { project_name: "ok" })).toBe("JOB-000017");
  });

  it("new_client over-long fields → 400 (every body string reaching D1 is bounded)", async () => {
    const res = await j(admin, "/api/fieldops/job", { project_name: "X", safety_cc: ["cc@x.com"], new_client: { name: "Acme Co", email: "x".repeat(321) } });
    expect(res.status).toBe(400);
  });

  it("counter_unavailable → 500 fail-closed when the job_counter ROW is missing (no malformed id)", async () => {
    await env.DB.prepare("DELETE FROM job_counter").run(); // seed row gone, table present
    const res = await j(admin, "/api/fieldops/job", { project_name: "X", safety_cc: ["cc@x.com"] });
    expect(res.status).toBe(500);
    expect(((await res.json()) as any).error).toBe("counter_unavailable");
    expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM jobs").first<{ n: number }>()).toMatchObject({ n: 0 });
    // beforeEach's INSERT OR REPLACE restores the row for the next test.
  });

  it("counter_unavailable → 500 when the job_counter TABLE is missing (0022 not applied before deploy)", async () => {
    // The literal deploy-order fault: D1 throws "no such table" — allocateJobNumber catches it and
    // collapses to the SAME clean counter_unavailable (not an opaque internal_error). Fail-closed.
    await env.DB.prepare("DROP TABLE job_counter").run();
    const res = await j(admin, "/api/fieldops/job", { project_name: "X", safety_cc: ["cc@x.com"] });
    expect(res.status).toBe(500);
    expect(((await res.json()) as any).error).toBe("counter_unavailable");
    expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM jobs").first<{ n: number }>()).toMatchObject({ n: 0 });
    // beforeEach's CREATE TABLE IF NOT EXISTS + INSERT OR REPLACE restores it for later tests.
  });
});

// TOMBSTONE (operator-approved deletion, 2026-07-03): the suites for POST /:job_id/close (the thin
// lifecycle='inactive' alias) and POST /:job_id/progress were deleted with their routes (zero
// SPA/Python callers; git history has both). The close SEMANTICS (inactive → active=0 +
// status='closed' + audit + idempotent re-set) are covered through /lifecycle below.

// ─────────────────────────────────────────────────────────────────────────────
// P2.5 Slice 1 — SoR routing fields on create + the lifecycle / contacts routes with the mirror
// version-vector dirty-flag. (Slice 6: the client-supplied job_id is ignored; the server assigns it.)
// ─────────────────────────────────────────────────────────────────────────────
describe("P2.5 — SoR create + lifecycle + contacts (version vector)", () => {
  const FULL = {
    project_name: "Solar Ridge",
    address: "1 Solar Way",
    stakeholder_name: "Stake Holder",
    stakeholder_email: "stake@x.com",
    safety_contact_name: "Sam Safety",
    safety_contact_email: "safety@x.com",
    safety_cc: ["sc1@x.com", "sc2@x.com"],
    progress_contact_name: "Pat Progress",
    progress_contact_email: "prog@x.com",
    progress_cc: ["pc1@x.com"],
  };

  it("persists the full routing SoR + lifecycle='active' + mirror_version=1 + dirty", async () => {
    const id = await createOk(admin, FULL);
    const row = await jobRow(id);
    expect(row.address).toBe("1 Solar Way");
    expect(row.safety_contact_email).toBe("safety@x.com");
    expect(JSON.parse(row.safety_cc)).toEqual(["sc1@x.com", "sc2@x.com"]);
    expect(JSON.parse(row.progress_cc)).toEqual(["pc1@x.com"]);
    expect(row.lifecycle).toBe("active");
    expect(row.mirror_version).toBe(1);
    expect(row.sync_state).toBe("pending");
  });

  it("rejects a malformed CC (not email-shaped) and an over-cap CC array", async () => {
    expect((await j(admin, "/api/fieldops/job", { ...FULL, safety_cc: ["not-an-email"] })).status).toBe(400);
    expect((await j(admin, "/api/fieldops/job", { ...FULL, progress_cc: ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com", "f@x.com"] })).status).toBe(400);
  });

  it("/lifecycle sets lifecycle + derived active + bumps the mirror version + re-dirties", async () => {
    const id = await createOk(admin, FULL); // mirror_version=1, sync_state pending
    // Simulate the daemon having mirrored it clean, then change lifecycle.
    await env.DB.prepare("UPDATE jobs SET sync_state='synced', safety_mirrored_version=1, progress_mirrored_version=1 WHERE job_id=?").bind(id).run();
    // Was 'archived' before Track 6 PR-0; that value is now refused here (see the next test).
    const res = await j(admin, `/api/fieldops/job/${id}/lifecycle`, { lifecycle: "inactive" });
    expect(res.status, await res.clone().text()).toBe(200);
    const row = await jobRow(id);
    expect(row.lifecycle).toBe("inactive");
    expect(row.active).toBe(0); // only 'active' lifecycle keeps active=1
    expect(row.mirror_version).toBe(2); // bumped
    expect(row.sync_state).toBe("pending"); // re-dirtied for the daemon
    expect((await audits("job_lifecycle")).length).toBe(1);
  });

  // Track 6 PR-0 — the disarm fence.
  //
  // Setting lifecycle='archived' here used to be accepted, and acceptance was a live hazard: the
  // write re-dirtied the row and the Mac-side mirror then relocated FOUR tracker sheets out of the
  // job's progress folder, unconfirmed and un-retryable, while the UI re-displayed the job as
  // "Inactive". Archiving is a heavyweight cross-system relocation and needs its own confirmed
  // route. 409 (not 400) because the intent is valid and the route is wrong.
  it("/lifecycle REFUSES 'archived' with 409 use_archive_route and writes NOTHING", async () => {
    const id = await createOk(admin, FULL);
    await env.DB.prepare("UPDATE jobs SET sync_state='synced' WHERE job_id=?").bind(id).run();
    const before = await jobRow(id);

    const res = await j(admin, `/api/fieldops/job/${id}/lifecycle`, { lifecycle: "archived" });
    expect(res.status).toBe(409);
    expect((await res.json() as { error: string }).error).toBe("use_archive_route");

    // The refusal must be total — no lifecycle change, no version bump, no re-dirty, no audit row.
    const after = await jobRow(id);
    expect(after.lifecycle).toBe(before.lifecycle);
    expect(after.active).toBe(before.active);
    expect(after.status).toBe(before.status);
    expect(after.mirror_version).toBe(before.mirror_version);
    expect(after.sync_state).toBe("synced");
    expect((await audits("job_lifecycle")).length).toBe(0);
  });

  it("/lifecycle still accepts the two settable values (no lockout from the archived fence)", async () => {
    const id = await createOk(admin, FULL);
    expect((await j(admin, `/api/fieldops/job/${id}/lifecycle`, { lifecycle: "inactive" })).status).toBe(200);
    expect((await j(admin, `/api/fieldops/job/${id}/lifecycle`, { lifecycle: "active" })).status).toBe(200);
    expect((await jobRow(id)).lifecycle).toBe("active");
  });

  it("/lifecycle rejects an invalid value; { lifecycle: 'inactive' } is the close path (status='closed', idempotent, unknown → 404)", async () => {
    const id = await createOk(admin, FULL);
    expect((await j(admin, `/api/fieldops/job/${id}/lifecycle`, { lifecycle: "bogus" })).status).toBe(400);
    expect((await j(admin, "/api/fieldops/job/NOPE/lifecycle", { lifecycle: "inactive" })).status).toBe(404);
    expect((await j(admin, `/api/fieldops/job/${id}/lifecycle`, { lifecycle: "inactive" })).status).toBe(200);
    // Re-closing is an idempotent 200 (re-dirties + bumps the version), NOT a 409 (TOCTOU-era guard).
    expect((await j(admin, `/api/fieldops/job/${id}/lifecycle`, { lifecycle: "inactive" })).status).toBe(200);
    const row = await jobRow(id);
    expect(row.lifecycle).toBe("inactive");
    expect(row.active).toBe(0);
    expect(row.status).toBe("closed"); // legacy status derived by the shared setter
    expect(row.sync_state).toBe("pending");
    expect((await audits("job_lifecycle")).length).toBe(2);
  });

  it("/contacts edits routing + bumps the mirror version (job_id/lifecycle untouched)", async () => {
    const id = await createOk(admin, FULL);
    await env.DB.prepare("UPDATE jobs SET sync_state='synced' WHERE job_id=?").bind(id).run();
    const res = await j(admin, `/api/fieldops/job/${id}/contacts`, { ...FULL, progress_contact_email: "newprog@x.com" });
    expect(res.status, await res.clone().text()).toBe(200);
    const row = await jobRow(id);
    expect(row.progress_contact_email).toBe("newprog@x.com");
    expect(row.lifecycle).toBe("active"); // untouched
    expect(row.mirror_version).toBe(2);
    expect(row.sync_state).toBe("pending");
  });

  it("/lifecycle + /contacts gate on cap.jobtracker.manage (submitter → 403)", async () => {
    const id = await createOk(admin, FULL);
    expect((await j(submitter, `/api/fieldops/job/${id}/lifecycle`, { lifecycle: "inactive" })).status).toBe(403);
    expect((await j(submitter, `/api/fieldops/job/${id}/contacts`, FULL)).status).toBe(403);
  });

  it("W5: edit routes REFUSE a smartsheet-origin job (no cross-origin corruption)", async () => {
    await seedJob("SS-9", "active"); // origin='smartsheet'
    // Payload is 'inactive', not 'archived': this test asserts the origin='portal' FENCE, and
    // 'archived' is now refused at the route (409 use_archive_route) BEFORE any row lookup, which
    // would mask the very thing under test. That ordering is deliberate — route-shape checks run
    // before DB work, like every other validation here — and it leaks nothing, because the 409 is
    // identical for any job id, existent or not.
    expect((await j(admin, "/api/fieldops/job/SS-9/lifecycle", { lifecycle: "inactive" })).status).toBe(404);
    expect((await j(admin, "/api/fieldops/job/SS-9/contacts", FULL)).status).toBe(404);
    const row = await jobRow("SS-9");
    expect(row.lifecycle).toBe("active"); // untouched (the origin='portal' scope refused every edit)
    expect(row.address).toBe("");
    expect(row.mirror_version).toBe(0);
  });

  it("W2: /lifecycle with a null JSON body → 400 (not a 500)", async () => {
    const id = await createOk(admin, FULL);
    expect((await j(admin, `/api/fieldops/job/${id}/lifecycle`, null)).status).toBe(400);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 0057 — the Evergreen YYYY.NNN job number + structured address (2026-07-20).
// ─────────────────────────────────────────────────────────────────────────────
describe("0057 — job_no + structured address", () => {
  it("create persists job_no + city/state/zip; /api/jobs + ship-to + detail serve them", async () => {
    const jobId = await createOk(admin, {
      project_name: "Coker",
      job_no: "2026.123",
      address: "100 Coker Rd",
      address_city: "Rockford",
      address_state: "il", // normalized to uppercase server-side
      address_zip: "61101",
    });
    const row = await jobRow(jobId);
    expect(row.job_no).toBe("2026.123");
    expect(row.address).toBe("100 Coker Rd");
    expect(row.address_city).toBe("Rockford");
    expect(row.address_state).toBe("IL");
    expect(row.address_zip).toBe("61101");

    // The dropdown feed carries job_no (the builders' autofill source).
    const jobs = ((await (await call("/api/jobs", { headers: { Cookie: admin } })).json()) as any).jobs;
    const mine = jobs.find((x: any) => x.job_id === jobId);
    expect(mine.job_no).toBe("2026.123");

    // The detail header serves job_no + the routing block (the editor's seed).
    const detail = (await (
      await call(`/api/fieldops/jobs/${jobId}`, { headers: { Cookie: admin } })
    ).json()) as any;
    expect(detail.job.job_no).toBe("2026.123");
    expect(detail.job.routing.address_city).toBe("Rockford");
    expect(detail.job.routing.address_state).toBe("IL");
  });

  it("ship-to serves the STORED number + structured city/state/zip (prefix parse stays the fallback)", async () => {
    await provision("po.admin", "password123", "admin");
    const poAdmin = await login("po.admin", "password123");
    const jobId = await createOk(admin, {
      project_name: "Coker", // NO prefix in the name — the stored field must win
      job_no: "2026.123",
      address: "100 Coker Rd",
      address_city: "Rockford",
      address_state: "IL",
      address_zip: "61101",
    });
    const s = (await (
      await call(`/api/po/jobs/${jobId}/ship-to`, { headers: { Cookie: poAdmin } })
    ).json()) as any;
    expect(s.job_no).toBe("2026.123");
    expect(s.ship_to_address).toBe("100 Coker Rd");
    expect(s.ship_to_city).toBe("Rockford");
    expect(s.ship_to_state).toBe("IL");
    expect(s.ship_to_zip).toBe("61101");
  });

  it("a malformed job_no or state is refused loudly (400), never stored mangled", async () => {
    expect(
      (await j(admin, "/api/fieldops/job", { project_name: "X", job_no: "26.123" })).status,
    ).toBe(400);
    expect(
      (await j(admin, "/api/fieldops/job", { project_name: "X", address_state: "Illinois" })).status,
    ).toBe(400);
  });

  // ── 0064: the three-segment Evergreen identifier ──────────────────────────────────────────
  // Evergreen numbers a job SITE as YYYY.NNN.S — MH405 is 2026.384.1, OG593 is 2026.384.2: one
  // project, two sites. The route SPLITS the typed identifier so job_no stays two-segment (every
  // other job_no validator and both Mac-side document-number parsers depend on that) and the site
  // lands in site_phase, the field D7 numbering already carries.
  it("0064: a three-segment job_no is SPLIT into (job_no, site_phase)", async () => {
    const id = await createOk(admin, { project_name: "MH405", job_no: "2026.384.1" });
    const row = await jobRow(id);
    expect(row.job_no).toBe("2026.384"); // two-segment, as every downstream validator expects
    expect(row.site_phase).toBe(1);
  });

  it("0064: same project, different sites → same job_no, different site_phase", async () => {
    const mh = await createOk(admin, { project_name: "MH405", job_no: "2026.384.1" });
    const og = await createOk(admin, { project_name: "OG593", job_no: "2026.384.2" });
    const [a, b] = [await jobRow(mh), await jobRow(og)];
    expect(a.job_no).toBe(b.job_no); // 2026.384 — the shared PROJECT number
    expect([a.site_phase, b.site_phase]).toEqual([1, 2]); // the sites are what differ
  });

  it("0064: a two-segment job_no still stores site_phase 0 (no site breakdown)", async () => {
    const id = await createOk(admin, { project_name: "Legacy", job_no: "2026.123" });
    const row = await jobRow(id);
    expect(row.job_no).toBe("2026.123");
    expect(row.site_phase).toBe(0);
  });

  it("0064: a job created WITHOUT a job_no defaults to site_phase 0, never null", async () => {
    const row = await jobRow(await createOk(admin, { project_name: "NoNumber" }));
    expect(row.job_no).toBe("");
    expect(row.site_phase).toBe(0); // NOT NULL DEFAULT 0 — safe to read without a COALESCE
  });

  it("0064: malformed third segments are refused, never silently truncated to the project number", async () => {
    // The failure mode this guards: accepting the string and storing only "2026.384" would file
    // MH405's documents under a number that belongs to no particular site.
    for (const bad of ["2026.384.", "2026.384.1.2", "2026.384.x", "2026.384.10000", "2026.384 .1"]) {
      const res = await j(admin, "/api/fieldops/job", { project_name: "Bad", job_no: bad, safety_cc: ["c@x.com"] });
      expect(res.status, `expected 400 for job_no ${JSON.stringify(bad)}`).toBe(400);
      expect(((await res.json()) as any).error).toBe("invalid_job_no");
    }
    expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM jobs").first<{ n: number }>()).toMatchObject({ n: 0 });
  });

  it("0064: site_phase 9999 is accepted (the D7 ceiling) and 10000 is not", async () => {
    const row = await jobRow(await createOk(admin, { project_name: "Edge", job_no: "2026.384.9999" }));
    expect(row.site_phase).toBe(9999);
    const over = await j(admin, "/api/fieldops/job", { project_name: "Over", job_no: "2026.384.10000", safety_cc: ["c@x.com"] });
    expect(over.status).toBe(400);
  });

  it("0064: the /contacts EDIT route re-splits too — changing the site updates site_phase", async () => {
    const id = await createOk(admin, { project_name: "Movable", job_no: "2026.384.1" });
    expect((await j(admin, `/api/fieldops/job/${id}/contacts`, { job_no: "2026.384.2" })).status).toBe(200);
    const row = await jobRow(id);
    expect(row.job_no).toBe("2026.384");
    expect(row.site_phase).toBe(2);
    // Clearing the number clears BOTH halves — no orphan site left behind on the row.
    expect((await j(admin, `/api/fieldops/job/${id}/contacts`, { address: "1 Main St" })).status).toBe(200);
    const cleared = await jobRow(id);
    expect(cleared.job_no).toBe("");
    expect(cleared.site_phase).toBe(0);
  });

  it("/contacts optional project_name: present renames, ABSENT leaves it unchanged, blank is 400", async () => {
    const jobId = await createOk(admin, { project_name: "Old Name", job_no: "2026.123" });
    // Absent → unchanged (the routing full-overwrite does NOT extend to the name).
    expect((await j(admin, `/api/fieldops/job/${jobId}/contacts`, { address: "1 Main St" })).status).toBe(200);
    let row = await jobRow(jobId);
    expect(row.project_name).toBe("Old Name");
    // Present → renamed (+ still re-dirtied for the mirror).
    expect((await j(admin, `/api/fieldops/job/${jobId}/contacts`, { project_name: "New Name" })).status).toBe(200);
    row = await jobRow(jobId);
    expect(row.project_name).toBe("New Name");
    expect(row.sync_state).toBe("pending");
    // Blank/whitespace → 400 (a name can never be blanked).
    expect((await j(admin, `/api/fieldops/job/${jobId}/contacts`, { project_name: "  " })).status).toBe(400);
  });

  it("the detail routing block is cap.jobtracker.manage-ONLY — read tier gets null (job_no still served)", async () => {
    const jobId = await createOk(admin, {
      project_name: "Coker",
      job_no: "2026.123",
      stakeholder_email: "owner@client.example",
    });
    // submitter holds cap.jobtracker.read (0013) but NOT manage — the send-recipient/CC
    // block must be withheld (least-privilege; adversarial review 2026-07-20).
    const asSubmitter = (await (
      await call(`/api/fieldops/jobs/${jobId}`, { headers: { Cookie: submitter } })
    ).json()) as any;
    expect(asSubmitter.job.routing).toBeNull();
    expect(asSubmitter.job.job_no).toBe("2026.123"); // the document-facing number stays visible
    expect(JSON.stringify(asSubmitter)).not.toContain("owner@client.example");
    const asAdmin = (await (
      await call(`/api/fieldops/jobs/${jobId}`, { headers: { Cookie: admin } })
    ).json()) as any;
    expect(asAdmin.job.routing.stakeholder_email).toBe("owner@client.example");
  });

  it("/contacts is a FULL OVERWRITE: an ABSENT key clears the stored value (the SPA's clear gesture)", async () => {
    const jobId = await createOk(admin, {
      project_name: "X",
      job_no: "2026.123",
      address_city: "Rockford",
    });
    const res = await j(admin, `/api/fieldops/job/${jobId}/contacts`, { address: "1 Main St" });
    expect(res.status).toBe(200);
    const row = await jobRow(jobId);
    expect(row.address).toBe("1 Main St");
    expect(row.job_no).toBe("");        // absent → '' — the documented clear semantics
    expect(row.address_city).toBe("");
  });

  it("/contacts round-trips the 0057 fields (edit is how a legacy job gains its number)", async () => {
    const jobId = await createOk(admin, { project_name: "Legacy Job" });
    const res = await j(admin, `/api/fieldops/job/${jobId}/contacts`, {
      job_no: "2025.007",
      address: "1 Main St",
      address_city: "Peoria",
      address_state: "IL",
      address_zip: "61602",
    });
    expect(res.status).toBe(200);
    const row = await jobRow(jobId);
    expect(row.job_no).toBe("2025.007");
    expect(row.address_city).toBe("Peoria");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Safety CC on create — OPTIONAL since 2026-08-11 (operator decision). It was required-on-create
// from 2026-07-24 until then; the guard is gone from both the Worker and the SPA. The CC BOUNDS
// (≤MAX_CC, email-shaped, length-capped) are unchanged. Case (i) is the inverted regression: the
// exact three bodies that used to 400 must now succeed.
// ─────────────────────────────────────────────────────────────────────────────
describe("Safety CC optional-on-create + delivery-contacts read route", () => {
  it("(i) INVERTED: create with an empty safety_cc now SUCCEEDS (the create-only guard was removed)", async () => {
    // Absent key — the body that used to return 400 safety_cc_required.
    const noKey = await j(admin, "/api/fieldops/job", { project_name: "NoCC" });
    expect(noKey.status, await noKey.clone().text()).toBe(201);
    expect(((await noKey.json()) as any).job_id).toBe("JOB-000017");
    // Explicit empty array (parseCc → []).
    const emptyArr = await j(admin, "/api/fieldops/job", { project_name: "NoCC2", safety_cc: [] });
    expect(emptyArr.status).toBe(201);
    // A whitespace-only entry is skipped by parseCc → still empty → still fine.
    const blank = await j(admin, "/api/fieldops/job", { project_name: "NoCC3", safety_cc: ["   "] });
    expect(blank.status).toBe(201);
    // All three really landed, and each stored an EMPTY CC list rather than a mangled one.
    expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM jobs").first<{ n: number }>()).toMatchObject({ n: 3 });
    expect(JSON.parse((await jobRow("JOB-000017")).safety_cc)).toEqual([]);
    // The counter advanced once per create — no number was burned or skipped.
    expect(((await blank.json()) as any).job_id).toBe("JOB-000019");
  });

  it("(i-b) the CC BOUNDS survive the guard removal — over-cap and malformed are still 400", async () => {
    // >MAX_CC (5) entries.
    const overCap = await j(admin, "/api/fieldops/job", {
      project_name: "TooMany",
      safety_cc: ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com", "f@x.com"],
    });
    expect(overCap.status).toBe(400);
    expect(((await overCap.json()) as any).error).toBe("invalid_cc");
    // A non-email-shaped entry.
    const malformed = await j(admin, "/api/fieldops/job", { project_name: "BadCc", safety_cc: ["not-an-email"] });
    expect(malformed.status).toBe(400);
    expect(((await malformed.json()) as any).error).toBe("invalid_cc");
    // Neither burned a row.
    expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM jobs").first<{ n: number }>()).toMatchObject({ n: 0 });
  });

  it("(ii) create with ≥1 valid safety_cc → 201 + the row stores the CC", async () => {
    const id = await createOk(admin, { project_name: "HasCC", safety_cc: ["cc1@x.com", "cc2@x.com"] });
    const row = await jobRow(id);
    expect(JSON.parse(row.safety_cc)).toEqual(["cc1@x.com", "cc2@x.com"]);
  });

  it("(iii) REGRESSION: the /contacts EDIT route still ACCEPTS an empty safety_cc", async () => {
    const id = await createOk(admin, { project_name: "EditMe", safety_cc: ["cc1@x.com"] });
    // An edit that omits safety_cc (full-overwrite → clears it) must SUCCEED — never 400.
    const res = await j(admin, `/api/fieldops/job/${id}/contacts`, { address: "1 Main St" });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(JSON.parse((await jobRow(id)).safety_cc)).toEqual([]); // blanked, as the edit path allows
    // An explicit empty array also succeeds.
    const res2 = await j(admin, `/api/fieldops/job/${id}/contacts`, { safety_cc: [] });
    expect(res2.status).toBe(200);
  });

  it("(iv) GET /api/fieldops/delivery-contacts serves the config to a jobtracker-manage holder", async () => {
    const res = await g(admin, "/api/fieldops/delivery-contacts");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as { delivery_contacts: unknown[] };
    // Served-equals-source (drift check), not a pinned literal.
    expect(body.delivery_contacts).toEqual(deliveryContactsConfig.contacts);
  });

  it("(iv) GET /api/fieldops/delivery-contacts is rejected without the cap (submitter → 403) and without a session (anon → 401)", async () => {
    expect((await g(submitter, "/api/fieldops/delivery-contacts")).status).toBe(403);
    expect((await call("/api/fieldops/delivery-contacts")).status).toBe(401);
  });
});
