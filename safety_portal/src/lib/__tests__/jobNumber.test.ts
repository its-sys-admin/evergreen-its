import { describe, it, expect } from "vitest";
import { formatJobNumber, splitJobNumber, JOB_NUMBER_MAX_LENGTH } from "../jobNumber";

// The Evergreen identifier `YYYY.NNN.S` is stored SPLIT (jobs.job_no + jobs.site_phase,
// migration 0064) and rejoined for display. These two helpers are that seam, so what matters
// is that they are a matched pair and that they agree with the Worker's own JOB_NO_INPUT_RE
// (safety_portal/worker/fieldops_job_write.ts) about which strings are legal.

describe("formatJobNumber", () => {
  it("joins the stored pair into the identifier the operator typed", () => {
    expect(formatJobNumber("2026.384", 1)).toBe("2026.384.1");
    expect(formatJobNumber("2026.384", 2)).toBe("2026.384.2");
    expect(formatJobNumber("2025.201", 3)).toBe("2025.201.3");
  });

  it("omits the site segment when there is no site breakdown (0 = unset, the D7 default)", () => {
    expect(formatJobNumber("2026.123", 0)).toBe("2026.123");
  });

  it("returns '' for an unassigned number regardless of site_phase", () => {
    // Guards a real display bug: a bare ".3" rendering for a job with no number yet.
    expect(formatJobNumber("", 3)).toBe("");
    expect(formatJobNumber(null, 3)).toBe("");
    expect(formatJobNumber(undefined, 0)).toBe("");
  });

  it("tolerates a null/undefined site_phase from a pre-0064 payload", () => {
    expect(formatJobNumber("2026.384", null)).toBe("2026.384");
    expect(formatJobNumber("2026.384", undefined)).toBe("2026.384");
  });
});

describe("splitJobNumber", () => {
  it("splits a three-segment identifier into project number + site", () => {
    expect(splitJobNumber("2026.384.1")).toEqual({ job_no: "2026.384", site_phase: 1 });
    expect(splitJobNumber("2026.391.1")).toEqual({ job_no: "2026.391", site_phase: 1 });
  });

  it("treats a two-segment identifier as site_phase 0", () => {
    expect(splitJobNumber("2026.123")).toEqual({ job_no: "2026.123", site_phase: 0 });
  });

  it("accepts empty as 'not yet assigned' — matching the Worker, which allows ''", () => {
    expect(splitJobNumber("")).toEqual({ job_no: "", site_phase: 0 });
    expect(splitJobNumber("   ")).toEqual({ job_no: "", site_phase: 0 });
  });

  it("trims surrounding whitespace before matching", () => {
    expect(splitJobNumber("  2026.384.1  ")).toEqual({ job_no: "2026.384", site_phase: 1 });
  });

  it("REFUSES malformed input rather than truncating it to the project number", () => {
    // The whole point: a bad identifier must never quietly become a valid-looking one for a
    // different site. Every case here would have been silently accepted by a prefix parse.
    for (const bad of [
      "2026.384.", // trailing dot
      "2026.384.1.2", // four segments
      "2026.384.x", // non-numeric site
      "26.384.1", // two-digit year
      "2026.38.1", // short project number
      "2026.3840.1", // long project number
      "2026.384 .1", // internal space
      "2026.384.10000", // site over the 9999 D7 ceiling
      "folder-2026.384", // a filename tag, not a number
      "2026.384.1 Coker", // the number plus a name
    ]) {
      expect(splitJobNumber(bad), `expected null for ${JSON.stringify(bad)}`).toBeNull();
    }
  });

  it("accepts the 9999 ceiling exactly", () => {
    expect(splitJobNumber("2026.384.9999")).toEqual({ job_no: "2026.384", site_phase: 9999 });
  });
});

describe("the pair round-trips", () => {
  it("split → format returns the original identifier", () => {
    for (const s of ["2026.384.1", "2026.384.2", "2025.201.3", "2026.391.1", "2026.123", ""]) {
      const parts = splitJobNumber(s);
      expect(parts).not.toBeNull();
      expect(formatJobNumber(parts!.job_no, parts!.site_phase)).toBe(s);
    }
  });

  it("a typed '.0' normalises away — 2026.384.0 and 2026.384 are the same identifier", () => {
    // Documented consequence of site_phase 0 meaning "no site", not "site zero".
    expect(formatJobNumber(...(Object.values(splitJobNumber("2026.384.0")!) as [string, number]))).toBe("2026.384");
  });

  it("the input maxLength admits the longest legal identifier", () => {
    const longest = "2026.384.9999";
    expect(longest.length).toBe(JOB_NUMBER_MAX_LENGTH);
    expect(splitJobNumber(longest)).not.toBeNull();
  });
});
