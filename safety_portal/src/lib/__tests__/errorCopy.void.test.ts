/**
 * Plain-language copy for the receipt-event VOID codes (migration 0080).
 *
 * tests/test_error_copy_parity.py is the canonical gate — it greps every `error: "…"` the
 * fieldops_*.ts routes return and fails on any without an ERROR_COPY entry. It only bites once
 * the Worker half has landed, though, and the SPA half ships these codes' UI in the same change.
 * This pins the eight codes the void contract names so the SPA can never render the humanized
 * "Something went wrong (already voided). Please try again." fallback for them — advice that is
 * actively false for every one of these: retrying a double void or a second correction can only
 * fail the same way.
 */
import { describe, expect, it } from "vitest";
import { ERROR_COPY, errorText } from "../errorCopy";

const VOID_CODES = [
  "reason_required",
  "invalid_reason",
  "already_voided",
  "job_not_active",
  "supersedes_not_voided",
  "unknown_supersedes",
  "already_corrected",
  "invalid_supersedes_id",
] as const;

describe("void + correction error copy", () => {
  it.each(VOID_CODES)("%s has its own plain-language copy, not the generic fallback", (code) => {
    expect(ERROR_COPY[code]).toBeTruthy();
    expect(errorText(code)).not.toContain("Something went wrong");
  });

  it("says Void, never Clear (operator decision D2)", () => {
    for (const code of VOID_CODES) expect(ERROR_COPY[code] ?? "").not.toMatch(/\bclear/i);
  });
});

// D11 (2026-10-21): removing a material line that still has deliveries COUNTING is refused
// (409 line_has_deliveries). The copy has to say what to do — the refusal is permanent until the
// marks are voided, so "try again" (the generic fallback) would send the office round in a loop.
describe("remove-line refusal copy (D11)", () => {
  it("line_has_deliveries tells the office to void the marks first", () => {
    expect(errorText("line_has_deliveries")).toBe(
      "This line has deliveries recorded. Void them first, then remove the line.",
    );
  });
});
