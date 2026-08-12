import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, g, p, seedJob } from "./helpers";
import { pacificDateString } from "../worker/fieldops_recurrence";

// ─────────────────────────────────────────────────────────────────────────────
// Job payments (ADR-0006 decision 10, PR-7) — worker/fieldops_payments.ts over
// migration 0073. Real workerd + D1, no route mocks.
//
// The properties under test are the ones the pure suite (payments_derive.test.ts)
// structurally cannot reach: the ADMIN-ONLY capability wall in BOTH directions
// (every route 403s manager+submitter AND payment data appears in no other route's
// response — operator decision 4), the terms upsert + bounds, the same-client
// prefill join (0014 clients), the due-date SNAPSHOT rule (a terms edit never moves
// it; only a submitted-date change recomputes it), the in-WHERE suspend guard with
// its honest 409, receipts moving the derived state on real rows, deactivate
// idempotency, and W4 mutation+audit atomicity.
// ─────────────────────────────────────────────────────────────────────────────

const SCHEDULE_BEARER = "test-schedule-token";

let admin = "";
let manager = "";
let sub = "";

/** PUT with a session cookie (helpers has no PUT one-liner; terms upsert is the repo's
 *  first session PUT under /api/fieldops). */
const put = (cookie: string, path: string, body?: unknown): Promise<Response> =>
  call(path, { method: "PUT", cookie, body: body === undefined ? undefined : JSON.stringify(body) });

const TERMS_BODY = {
  job_id: "JOB-A",
  billing_cadence: "monthly",
  net_days: 30,
  nonpayment_notice_days: 10,
  intent_to_suspend_days: 14,
};

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_payment_receipts"),
    env.DB.prepare("DELETE FROM job_payment_cycles"),
    env.DB.prepare("DELETE FROM job_payment_terms"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM clients"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
  await provision("admin.one", "pw-admin-1234", "admin");
  await provision("mgr.mike", "pw-manager-12", "manager");
  await provision("sub.sam", "pw-subby-1234", "submitter");
  admin = await login("admin.one", "pw-admin-1234");
  manager = await login("mgr.mike", "pw-manager-12");
  sub = await login("sub.sam", "pw-subby-1234");
  await seedJob("JOB-A", { projectName: "Deep Lake" });
  await seedJob("JOB-B");
});

async function saveTerms(over: Record<string, unknown> = {}, cookie = admin): Promise<Response> {
  return put(cookie, "/api/fieldops/payments/terms", { ...TERMS_BODY, ...over });
}

async function createCycle(over: Record<string, unknown> = {}, cookie = admin): Promise<number> {
  const res = await p(cookie, "/api/fieldops/payments/cycles", {
    job_id: "JOB-A", label: "PP #1", ...over,
  });
  expect(res.status, await res.clone().text()).toBe(201);
  return ((await res.json()) as { id: number }).id;
}

async function readPayments(jobId = "JOB-A"): Promise<{
  terms: Record<string, unknown> | null;
  cycles: (Record<string, unknown> & { receipts: Record<string, unknown>[] })[];
  project_name: string | null;
}> {
  const res = await g(admin, `/api/fieldops/payments?job_id=${jobId}`);
  expect(res.status, await res.clone().text()).toBe(200);
  return (await res.json()) as never;
}

describe("the capability wall (cap.payments.manage — ADMIN ONLY, operator decision 4)", () => {
  it("manager AND submitter are 403 on EVERY route; anon and the schedule bearer are 401", async () => {
    const routes: [string, (cookie: string) => Promise<Response>][] = [
      ["GET /payments", (c) => g(c, "/api/fieldops/payments?job_id=JOB-A")],
      ["GET /terms-default", (c) => g(c, "/api/fieldops/payments/terms-default?job_id=JOB-A")],
      ["PUT /terms", (c) => saveTerms({}, c)],
      ["POST /cycles", (c) => p(c, "/api/fieldops/payments/cycles", { job_id: "JOB-A", label: "X" })],
      ["POST /cycles/:id/edit", (c) => p(c, "/api/fieldops/payments/cycles/1/edit", { label: "X" })],
      ["POST /cycles/:id/notice", (c) => p(c, "/api/fieldops/payments/cycles/1/notice", { kind: "nonpayment" })],
      ["POST /cycles/:id/receipt", (c) => p(c, "/api/fieldops/payments/cycles/1/receipt", { amount_cents: 1 })],
      ["POST /cycles/:id/deactivate", (c) => p(c, "/api/fieldops/payments/cycles/1/deactivate")],
      ["POST /receipts/:id/deactivate", (c) => p(c, "/api/fieldops/payments/receipts/1/deactivate")],
    ];
    for (const [name, fn] of routes) {
      expect((await fn(manager)).status, `${name} as manager`).toBe(403);
      expect((await fn(sub)).status, `${name} as submitter`).toBe(403);
    }
    expect((await call("/api/fieldops/payments?job_id=JOB-A")).status).toBe(401);
    expect(
      (await call("/api/fieldops/payments?job_id=JOB-A", { bearer: SCHEDULE_BEARER })).status,
    ).toBe(401);
  });

  it("the cap gate is REAL — revoking cap.payments.manage from admin → 403 (capabilities resolve per-request)", async () => {
    await env.DB
      .prepare("DELETE FROM role_capabilities WHERE role_key = 'admin' AND capability_key = 'cap.payments.manage'")
      .run();
    try {
      expect((await g(admin, "/api/fieldops/payments?job_id=JOB-A")).status).toBe(403);
    } finally {
      await env.DB
        .prepare("INSERT OR IGNORE INTO role_capabilities (role_key, capability_key) VALUES ('admin', 'cap.payments.manage')")
        .run();
    }
    expect((await g(admin, "/api/fieldops/payments?job_id=JOB-A")).status).toBe(200);
  });

  it("payment data appears in NO other route's response (decision 4 — the leak direction)", async () => {
    // Real money on the job…
    expect((await saveTerms()).status).toBe(200);
    await createCycle({ invoice_submitted_date: "2026-06-01", invoice_amount_cents: 4_431_00 });
    // …and none of it in the job reads or the schedule read, even for the admin who
    // COULD read it through the payments route. Assert on the payments-only tokens: the
    // terms column names and the cents value would each betray a join gone wide.
    for (const path of [
      "/api/fieldops/jobs",
      "/api/fieldops/jobs/JOB-A",
      "/api/fieldops/schedule-tasks?job_id=JOB-A",
    ]) {
      const res = await g(admin, path);
      expect(res.status, path).toBe(200);
      const text = await res.text();
      for (const token of ["net_days", "nonpayment", "invoice_amount_cents", "443100", "job_payment"]) {
        expect(text, `${path} must not leak '${token}'`).not.toContain(token);
      }
    }
  });
});

describe("terms upsert (PUT /api/fieldops/payments/terms)", () => {
  it("creates, then UPDATES IN PLACE on re-PUT (ON CONFLICT job_id); audited; W9 — no usernames served", async () => {
    const res = await saveTerms();
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as { ok: boolean; terms: Record<string, unknown> };
    expect(body.terms).toMatchObject({
      job_id: "JOB-A", billing_cadence: "monthly", net_days: 30,
      nonpayment_notice_days: 10, intent_to_suspend_days: 14,
    });
    expect(body.terms).not.toHaveProperty("created_by");
    expect(body.terms).not.toHaveProperty("updated_by");

    const again = await saveTerms({ net_days: 45, notes: "retainage 10%" });
    expect(again.status).toBe(200);
    const rows = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_payment_terms WHERE job_id='JOB-A'")
      .first<{ n: number }>();
    expect(rows!.n).toBe(1); // upsert, never a second row
    const db = await env.DB
      .prepare("SELECT net_days, notes, created_by, updated_by FROM job_payment_terms WHERE job_id='JOB-A'")
      .first<Record<string, unknown>>();
    expect(db).toMatchObject({ net_days: 45, notes: "retainage 10%", created_by: "admin.one", updated_by: "admin.one" });
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='payment_terms_upsert'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(2);
  });

  it("bounds: each field refuses with its own code; unknown job 404; blank job_id 400", async () => {
    for (const [over, code] of [
      [{ net_days: -1 }, "invalid_net_days"],
      [{ net_days: 366 }, "invalid_net_days"],
      [{ net_days: 12.5 }, "invalid_net_days"],
      [{ net_days: "30" }, "invalid_net_days"],
      [{ nonpayment_notice_days: 0 }, "invalid_notice_days"],
      [{ nonpayment_notice_days: 366 }, "invalid_notice_days"],
      [{ intent_to_suspend_days: 0 }, "invalid_notice_days"],
      [{ billing_cadence: "weekly" }, "invalid_billing_cadence"],
      [{ billing_cadence_detail: "x".repeat(301) }, "invalid_cadence_detail"],
      [{ notes: "x".repeat(2001) }, "invalid_payment_note"],
    ] as const) {
      const res = await saveTerms(over as Record<string, unknown>);
      expect(res.status, JSON.stringify(over)).toBe(400);
      expect(await res.json()).toMatchObject({ error: code });
    }
    expect((await saveTerms({ job_id: "JOB-NOPE" })).status).toBe(404);
    expect((await saveTerms({ job_id: "" })).status).toBe(400);
  });
});

describe("same-client prefill (GET /api/fieldops/payments/terms-default — operator decision 6)", () => {
  /** Two jobs of one client with different terms, one clientless job. */
  async function seedClientJobs(): Promise<number> {
    const cid = (
      await env.DB.prepare("INSERT INTO clients (name) VALUES ('Acme Solar') RETURNING id").first<{ id: number }>()
    )!.id;
    await seedJob("JOB-C-OLD", { client_id: cid, createdAt: 1_000 });
    await seedJob("JOB-C-NEW", { client_id: cid, createdAt: 2_000, projectName: "Kestrel" });
    expect((await saveTerms({ job_id: "JOB-C-OLD", net_days: 45 })).status).toBe(200);
    expect((await saveTerms({ job_id: "JOB-C-NEW", net_days: 60 })).status).toBe(200);
    return cid;
  }

  it("returns the SAME client's most RECENT other job's terms, with the labels the button copy needs", async () => {
    const cid = await seedClientJobs();
    await seedJob("JOB-C-BLANK", { client_id: cid, createdAt: 3_000 });
    const res = await g(admin, "/api/fieldops/payments/terms-default?job_id=JOB-C-BLANK");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body).toMatchObject({
      terms: { net_days: 60, billing_cadence: "monthly" }, // JOB-C-NEW's (created_at 2000 > 1000)
      client_name: "Acme Solar",
      source_project_name: "Kestrel",
    });
    expect(body.terms).not.toHaveProperty("job_id"); // a prefill VALUE set, not a linked row
  });

  it("excludes the asking job's OWN terms (an edit screen must not offer the row to itself)", async () => {
    await seedClientJobs();
    const res = await g(admin, "/api/fieldops/payments/terms-default?job_id=JOB-C-NEW");
    expect((await res.json() as { terms: { net_days: number } }).terms.net_days).toBe(45); // the OLD job's
  });

  it("no client on the job → {terms:null}, 200 — never an error (decision 6)", async () => {
    const res = await g(admin, "/api/fieldops/payments/terms-default?job_id=JOB-A");
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ terms: null, client_name: null, source_project_name: null });
  });

  it("client with no OTHER termed job → terms:null but the client_name still serves", async () => {
    const cid = (
      await env.DB.prepare("INSERT INTO clients (name) VALUES ('Lone Client') RETURNING id").first<{ id: number }>()
    )!.id;
    await seedJob("JOB-LONE", { client_id: cid });
    const res = await g(admin, "/api/fieldops/payments/terms-default?job_id=JOB-LONE");
    expect(await res.json()).toMatchObject({ terms: null, client_name: "Lone Client" });
  });
});

describe("cycle create (POST /api/fieldops/payments/cycles)", () => {
  it("computes + STORES the due-date snapshot from the job's terms; seq walks max+10; audited", async () => {
    expect((await saveTerms()).status).toBe(200);
    const res = await p(admin, "/api/fieldops/payments/cycles", {
      job_id: "JOB-A", label: "Mobilization", invoice_submitted_date: "2026-06-01",
      invoice_amount_cents: 250_000,
    });
    expect(res.status, await res.clone().text()).toBe(201);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ ok: true, due_date: "2026-07-01" }); // net 30
    expect(body).not.toHaveProperty("note");
    expect(String(body.cycle_uuid)).toMatch(/^[0-9a-f-]{36}$/);

    const second = await createCycle({ label: "PP #2" });
    const rows = await env.DB
      .prepare("SELECT label, seq, due_date, created_by FROM job_payment_cycles ORDER BY seq")
      .all<Record<string, unknown>>();
    expect(rows.results).toEqual([
      expect.objectContaining({ label: "Mobilization", seq: 10, due_date: "2026-07-01", created_by: "admin.one" }),
      expect.objectContaining({ label: "PP #2", seq: 20, due_date: null }), // draft — no submitted date
    ]);
    expect(second).toBeGreaterThan(0);
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='payment_cycle_create'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(2);
  });

  it("a SUBMITTED cycle without terms lands due_date NULL with the explicit note (never a silent NULL)", async () => {
    const res = await p(admin, "/api/fieldops/payments/cycles", {
      job_id: "JOB-B", label: "PP #1", invoice_submitted_date: "2026-06-01",
    });
    expect(res.status).toBe(201);
    expect(await res.json()).toMatchObject({ ok: true, due_date: null, note: "no_terms_no_due_date" });
  });

  it("bounds: label / submitted date / amount each refuse with their own code; unknown job 404", async () => {
    for (const [body, code] of [
      [{ job_id: "JOB-A" }, "invalid_cycle_label"],
      [{ job_id: "JOB-A", label: "" }, "invalid_cycle_label"],
      [{ job_id: "JOB-A", label: "x".repeat(121) }, "invalid_cycle_label"],
      [{ job_id: "JOB-A", label: "X", invoice_submitted_date: "06/01/2026" }, "invalid_submitted_date"],
      [{ job_id: "JOB-A", label: "X", invoice_amount_cents: -1 }, "invalid_amount_cents"],
      [{ job_id: "JOB-A", label: "X", invoice_amount_cents: 19.99 }, "invalid_amount_cents"],
      [{ job_id: "JOB-A", label: "X", note: "x".repeat(2001) }, "invalid_payment_note"],
    ] as const) {
      const res = await p(admin, "/api/fieldops/payments/cycles", body as Record<string, unknown>);
      expect(res.status, JSON.stringify(body)).toBe(400);
      expect(await res.json()).toMatchObject({ error: code });
    }
    expect(
      (await p(admin, "/api/fieldops/payments/cycles", { job_id: "JOB-NOPE", label: "X" })).status,
    ).toBe(404);
  });

  it("GET /payments reads the whole card: terms + cycles in seq order + project name + derived state", async () => {
    expect((await saveTerms()).status).toBe(200);
    await createCycle({ label: "Draft cycle" });
    const data = await readPayments();
    expect(data.project_name).toBe("Deep Lake");
    expect(data.terms).toMatchObject({ net_days: 30 });
    expect(data.cycles).toHaveLength(1);
    expect(data.cycles[0]).toMatchObject({
      label: "Draft cycle", state: "draft", receipts: [], balance_cents: null,
    });
    // Another job's cycles never leak in.
    expect((await readPayments("JOB-B")).cycles).toHaveLength(0);
  });
});

describe("the due-date SNAPSHOT rule (ADR-0006 decision 10)", () => {
  it("a TERMS edit never moves a stored due date; a label-only edit keeps it too; a submitted-date change recomputes from CURRENT terms; clearing the date clears it", async () => {
    expect((await saveTerms()).status).toBe(200); // net 30
    const id = await createCycle({ invoice_submitted_date: "2026-06-01", invoice_amount_cents: 1000 });
    expect((await readPayments()).cycles[0].due_date).toBe("2026-07-01");

    // 1 — terms move to net 60: the invoiced cycle's snapshot must NOT move.
    expect((await saveTerms({ net_days: 60 })).status).toBe(200);
    expect((await readPayments()).cycles[0].due_date).toBe("2026-07-01");

    // 2 — an edit that does NOT touch the submitted date keeps the snapshot byte-for-byte
    //     (the snapshot updates only when ITS OWN input changes).
    const relabel = await p(admin, `/api/fieldops/payments/cycles/${id}/edit`, {
      label: "PP #1 (revised)", invoice_submitted_date: "2026-06-01", invoice_amount_cents: 1000,
    });
    expect(relabel.status, await relabel.clone().text()).toBe(200);
    expect(await relabel.json()).toMatchObject({ ok: true, due_date: "2026-07-01" });

    // 3 — the submitted date CHANGES: recompute from the CURRENT terms (now net 60).
    const resubmit = await p(admin, `/api/fieldops/payments/cycles/${id}/edit`, {
      label: "PP #1 (revised)", invoice_submitted_date: "2026-06-10", invoice_amount_cents: 1000,
    });
    expect(await resubmit.json()).toMatchObject({ due_date: "2026-08-09" }); // 06-10 + 60

    // 4 — clearing the submitted date (back to draft) clears the snapshot with it.
    const clear = await p(admin, `/api/fieldops/payments/cycles/${id}/edit`, {
      label: "PP #1 (revised)", invoice_amount_cents: 1000,
    });
    expect(await clear.json()).toMatchObject({ due_date: null });
    const row = await env.DB
      .prepare("SELECT invoice_submitted_date, due_date FROM job_payment_cycles WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ invoice_submitted_date: null, due_date: null });

    // Edits audited with from/to (audit_log is the history).
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='payment_cycle_edit'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(3);
  });

  it("edit 404s an unknown id and an INACTIVE cycle (in-WHERE active=1)", async () => {
    expect((await p(admin, "/api/fieldops/payments/cycles/999999/edit", { label: "X" })).status).toBe(404);
    const id = await createCycle();
    expect((await p(admin, `/api/fieldops/payments/cycles/${id}/deactivate`)).status).toBe(200);
    expect((await p(admin, `/api/fieldops/payments/cycles/${id}/edit`, { label: "X" })).status).toBe(404);
  });
});

describe("notices (POST /cycles/:id/notice) — recorded, never sent", () => {
  it("records nonpayment (explicit date), defaults today Pacific when omitted, re-post corrects the date; audited from/to", async () => {
    const id = await createCycle({ invoice_submitted_date: "2026-01-01" });
    const res = await p(admin, `/api/fieldops/payments/cycles/${id}/notice`, {
      kind: "nonpayment", date: "2026-06-05",
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await res.json()).toMatchObject({ ok: true, kind: "nonpayment", date: "2026-06-05" });

    // Re-post corrects (a correction is real).
    expect(
      (await p(admin, `/api/fieldops/payments/cycles/${id}/notice`, { kind: "nonpayment", date: "2026-06-06" }))
        .status,
    ).toBe(200);
    const audits = await env.DB
      .prepare("SELECT detail FROM audit_log WHERE action='payment_notice_nonpayment' ORDER BY id ASC")
      .all<{ detail: string }>();
    expect(audits.results).toHaveLength(2);
    expect(JSON.parse(audits.results![1].detail)).toMatchObject({ from: "2026-06-05", to: "2026-06-06" });

    // Omitted date → today PACIFIC (the Worker's clock, not the browser's).
    const id2 = await createCycle({ label: "PP #2" });
    const today = await p(admin, `/api/fieldops/payments/cycles/${id2}/notice`, { kind: "nonpayment" });
    expect(((await today.json()) as { date: string }).date).toBe(pacificDateString(Date.now()));
  });

  it("suspend WITHOUT a recorded nonpayment → honest 409 suspend_requires_nonpayment, nothing written", async () => {
    const id = await createCycle();
    const res = await p(admin, `/api/fieldops/payments/cycles/${id}/notice`, { kind: "suspend" });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "suspend_requires_nonpayment" });
    const row = await env.DB
      .prepare("SELECT suspend_notice_date FROM job_payment_cycles WHERE id = ?1")
      .bind(id)
      .first<{ suspend_notice_date: string | null }>();
    expect(row!.suspend_notice_date).toBeNull();
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='payment_notice_suspend'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(0); // W4: no write ⇒ no audit row
  });

  it("the suspend precondition is enforced IN-WHERE, not just at load time — the mutation itself refuses the race arm", async () => {
    // The TOCTOU window (a nonpayment-date clear lands AFTER the route's pre-load) can't
    // be interleaved through the single-threaded test worker, so this proves the guard
    // at its own level: the route's exact mutation WHERE, run against a cycle with NO
    // nonpayment date, must change ZERO rows. Mirrors the SQL in fieldops_payments.ts —
    // if that WHERE changes, change this with it.
    const id = await createCycle();
    const raced = await env.DB
      .prepare(
        `UPDATE job_payment_cycles
            SET suspend_notice_date = ?2, updated_by = ?3, updated_at = unixepoch()
          WHERE id = ?1 AND active = 1 AND nonpayment_notice_date IS NOT NULL`,
      )
      .bind(id, "2026-06-10", "racer")
      .run();
    expect(raced.meta.changes).toBe(0); // the invariant held atomically
    // …and the same statement WITH the precondition met still lands (not a lockout).
    await env.DB
      .prepare("UPDATE job_payment_cycles SET nonpayment_notice_date = '2026-06-01' WHERE id = ?1")
      .bind(id)
      .run();
    const ok = await env.DB
      .prepare(
        `UPDATE job_payment_cycles
            SET suspend_notice_date = ?2, updated_by = ?3, updated_at = unixepoch()
          WHERE id = ?1 AND active = 1 AND nonpayment_notice_date IS NOT NULL`,
      )
      .bind(id, "2026-06-10", "racer")
      .run();
    expect(ok.meta.changes).toBe(1);
  });

  it("suspend AFTER a recorded nonpayment lands; the derived state ladder follows on read", async () => {
    expect((await saveTerms()).status).toBe(200);
    const id = await createCycle({ invoice_submitted_date: "2020-01-01", invoice_amount_cents: 1000 });
    // Deep overdue, no notice recorded → the machine says only that the notice is DUE.
    expect((await readPayments()).cycles[0].state).toBe("nonpayment_notice_due");
    expect(
      (await p(admin, `/api/fieldops/payments/cycles/${id}/notice`, { kind: "nonpayment", date: "2020-03-01" }))
        .status,
    ).toBe(200);
    // Recorded long ago → the suspend clock (14d) has rung.
    expect((await readPayments()).cycles[0].state).toBe("suspension_notice_due");
    expect(
      (await p(admin, `/api/fieldops/payments/cycles/${id}/notice`, { kind: "suspend", date: "2020-03-20" }))
        .status,
    ).toBe(200);
    expect((await readPayments()).cycles[0].state).toBe("suspension_notice_sent");
  });

  it("bad kind / bad date / unknown id refuse typed", async () => {
    const id = await createCycle();
    const badKind = await p(admin, `/api/fieldops/payments/cycles/${id}/notice`, { kind: "reminder" });
    expect(badKind.status).toBe(400);
    expect(await badKind.json()).toMatchObject({ error: "invalid_notice_kind" });
    const badDate = await p(admin, `/api/fieldops/payments/cycles/${id}/notice`, {
      kind: "nonpayment", date: "06/05/2026",
    });
    expect(badDate.status).toBe(400);
    expect(await badDate.json()).toMatchObject({ error: "invalid_notice_date" });
    expect(
      (await p(admin, "/api/fieldops/payments/cycles/999999/notice", { kind: "nonpayment" })).status,
    ).toBe(404);
  });
});

describe("receipts (POST /cycles/:id/receipt) — append-only, the derived state follows", () => {
  it("partial → state unchanged with the balance outstanding; full → paid (+paid_late when the last receipt is past due)", async () => {
    expect((await saveTerms()).status).toBe(200); // net 30 → due 2020-01-31
    const id = await createCycle({ invoice_submitted_date: "2020-01-01", invoice_amount_cents: 100_000 });

    const partial = await p(admin, `/api/fieldops/payments/cycles/${id}/receipt`, {
      amount_cents: 40_000, received_date: "2020-02-15",
    });
    expect(partial.status, await partial.clone().text()).toBe(201);
    let view = (await readPayments()).cycles[0];
    expect(view.state).toBe("nonpayment_notice_due"); // deep overdue — a partial doesn't settle it
    expect(view.balance_cents).toBe(60_000);
    expect(view.receipts).toHaveLength(1);

    expect(
      (await p(admin, `/api/fieldops/payments/cycles/${id}/receipt`, {
        amount_cents: 60_000, received_date: "2020-03-01",
      })).status,
    ).toBe(201);
    view = (await readPayments()).cycles[0];
    expect(view).toMatchObject({ state: "paid", paid_late: true, balance_cents: 0 }); // 03-01 > due 01-31
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='payment_receipt_add'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(2);
  });

  it("paid ON TIME when the last receipt is at/before due; receipts default today Pacific", async () => {
    expect((await saveTerms()).status).toBe(200);
    // Submitted today → due 30 days out; a full receipt (defaulted to today) is early.
    const id = await createCycle({
      invoice_submitted_date: pacificDateString(Date.now()), invoice_amount_cents: 5_000,
    });
    const res = await p(admin, `/api/fieldops/payments/cycles/${id}/receipt`, { amount_cents: 5_000 });
    expect(res.status).toBe(201);
    const view = (await readPayments()).cycles[0];
    expect(view).toMatchObject({ state: "paid", paid_late: false });
    expect(view.receipts[0]).toMatchObject({ received_date: pacificDateString(Date.now()) });
    expect(view.receipts[0]).not.toHaveProperty("recorded_by"); // W9 posture
  });

  it("deactivating a receipt is the correction path — the state honestly reverts", async () => {
    expect((await saveTerms()).status).toBe(200);
    const id = await createCycle({ invoice_submitted_date: "2020-01-01", invoice_amount_cents: 1_000 });
    expect(
      (await p(admin, `/api/fieldops/payments/cycles/${id}/receipt`, {
        amount_cents: 1_000, received_date: "2020-02-01",
      })).status,
    ).toBe(201);
    expect((await readPayments()).cycles[0].state).toBe("paid");
    const rid = (await env.DB.prepare("SELECT id FROM job_payment_receipts").first<{ id: number }>())!.id;
    expect((await p(admin, `/api/fieldops/payments/receipts/${rid}/deactivate`)).status).toBe(200);
    const view = (await readPayments()).cycles[0];
    expect(view.state).not.toBe("paid");
    expect(view.balance_cents).toBe(1_000);
    expect(view.receipts).toHaveLength(0); // the tombstone stays out of the display
  });

  it("bounds: zero / negative / fractional / string amounts and a bad date refuse typed; unknown + inactive cycle 404", async () => {
    const id = await createCycle();
    for (const amount_cents of [0, -5, 19.99, "100"]) {
      const res = await p(admin, `/api/fieldops/payments/cycles/${id}/receipt`, { amount_cents });
      expect(res.status, JSON.stringify(amount_cents)).toBe(400);
      expect(await res.json()).toMatchObject({ error: "invalid_amount_cents" });
    }
    const badDate = await p(admin, `/api/fieldops/payments/cycles/${id}/receipt`, {
      amount_cents: 100, received_date: "02/01/2020",
    });
    expect(badDate.status).toBe(400);
    expect(await badDate.json()).toMatchObject({ error: "invalid_received_date" });
    expect(
      (await p(admin, "/api/fieldops/payments/cycles/999999/receipt", { amount_cents: 100 })).status,
    ).toBe(404);
    expect((await p(admin, `/api/fieldops/payments/cycles/${id}/deactivate`)).status).toBe(200);
    expect(
      (await p(admin, `/api/fieldops/payments/cycles/${id}/receipt`, { amount_cents: 100 })).status,
    ).toBe(404); // the guarded INSERT re-asserts active=1 in the batch itself
  });
});

describe("deactivate idempotency (cycles + receipts — the schedule-task shape)", () => {
  it("cycle: second call → 200 already_inactive, ONE audit row; the read excludes it; unknown → 404", async () => {
    const id = await createCycle();
    expect((await p(admin, `/api/fieldops/payments/cycles/${id}/deactivate`)).status).toBe(200);
    const again = await p(admin, `/api/fieldops/payments/cycles/${id}/deactivate`);
    expect(again.status).toBe(200);
    expect(await again.json()).toMatchObject({ ok: true, already_inactive: true });
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='payment_cycle_deactivate'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(1);
    expect((await readPayments()).cycles).toHaveLength(0);
    expect((await p(admin, "/api/fieldops/payments/cycles/999999/deactivate")).status).toBe(404);
  });

  it("receipt: same shape — idempotent, one audit, 404 on unknown", async () => {
    const id = await createCycle({ invoice_submitted_date: "2026-06-01" });
    expect(
      (await p(admin, `/api/fieldops/payments/cycles/${id}/receipt`, { amount_cents: 100 })).status,
    ).toBe(201);
    const rid = (await env.DB.prepare("SELECT id FROM job_payment_receipts").first<{ id: number }>())!.id;
    expect((await p(admin, `/api/fieldops/payments/receipts/${rid}/deactivate`)).status).toBe(200);
    const again = await p(admin, `/api/fieldops/payments/receipts/${rid}/deactivate`);
    expect(await again.json()).toMatchObject({ ok: true, already_inactive: true });
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='payment_receipt_deactivate'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(1);
    expect((await p(admin, "/api/fieldops/payments/receipts/999999/deactivate")).status).toBe(404);
  });
});
