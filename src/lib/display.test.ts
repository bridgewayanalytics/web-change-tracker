import { describe, it, expect } from "vitest";
import {
  getDisplayGroupName,
  getDisplayTitle,
  formatMeetingDate,
  formatDateOnly,
  formatTimeRangeET,
  getGroupShortDisplayName,
  cleanDisplayText,
} from "./display";

describe("getDisplayGroupName", () => {
  it("prefers NAIC Group (tree node) object name", () => {
    expect(
      getDisplayGroupName({
        "NAIC Group (tree node)": { name: "Life RBC Working Group" },
      })
    ).toBe("Life RBC Working Group");
    expect(
      getDisplayGroupName({
        "NAIC Group (tree node)": { Name: "Capital Adequacy TF" },
      })
    ).toBe("Capital Adequacy TF");
  });

  it("uses NAIC Group string when present", () => {
    expect(
      getDisplayGroupName({ "NAIC Group": "Financial Condition Committee" })
    ).toBe("Financial Condition Committee");
  });

  it("uses Organization when present", () => {
    expect(
      getDisplayGroupName({ Organization: "NAIC BWG" })
    ).toBe("NAIC BWG");
    expect(
      getDisplayGroupName({ Organization: { name: "Expanded Org" } })
    ).toBe("Expanded Org");
  });

  it("splits title at ' | ' and takes left part", () => {
    expect(
      getDisplayGroupName({
        title: "NAIC BWG | CLOs and ABS; Calendar Events with no Topic",
      })
    ).toBe("NAIC BWG");
    expect(
      getDisplayGroupName({
        title: "Life RBC WG | Some Topic; Another Topic",
      })
    ).toBe("Life RBC WG");
  });

  it("takes first segment before semicolon when no pipe", () => {
    expect(
      getDisplayGroupName({
        title: "CLOs and ABS; Calendar Events with no Topic",
      })
    ).toBe("CLOs and ABS");
    expect(
      getDisplayGroupName({
        title: "Life Actuarial Task Force; Misc Topics",
      })
    ).toBe("Life Actuarial Task Force");
  });

  it("returns full title when no delimiter", () => {
    expect(getDisplayGroupName({ title: "NAIC BWG" })).toBe("NAIC BWG");
    expect(getDisplayGroupName({ title: "Standalone Meeting" })).toBe(
      "Standalone Meeting"
    );
  });

  it("returns Untitled when empty", () => {
    expect(getDisplayGroupName({})).toBe("Untitled");
    expect(getDisplayGroupName({ title: "" })).toBe("Untitled");
  });

  it("handles messy title with pipe and semicolons", () => {
    expect(
      getDisplayGroupName({
        title: "NAIC BWG | CLOs and ABS; Calendar Events with no Topic; More",
      })
    ).toBe("NAIC BWG");
  });
});

describe("getDisplayTitle", () => {
  it("adds meeting type when available", () => {
    expect(
      getDisplayTitle({
        title: "NAIC BWG",
        "Meeting Type": "Working Group",
      })
    ).toBe("NAIC BWG | Working Group");
  });

  it("omits meeting type when not available", () => {
    expect(getDisplayTitle({ title: "NAIC BWG" })).toBe("NAIC BWG");
  });
});

describe("getGroupShortDisplayName", () => {
  it("keeps short codes as-is with NAIC prefix", () => {
    expect(getGroupShortDisplayName("NAIC BWG")).toBe("NAIC BWG");
    expect(getGroupShortDisplayName("NAIC LRBCWG")).toBe("NAIC LRBCWG");
    expect(getGroupShortDisplayName("NAIC LATF")).toBe("NAIC LATF");
  });

  it("converts long names to acronym", () => {
    expect(getGroupShortDisplayName("NAIC Big Working Group")).toBe("NAIC BWG");
    expect(getGroupShortDisplayName("NAIC Life Risk Based Capital Working Group")).toBe(
      "NAIC LRBCWG"
    );
  });

  it("returns — for empty", () => {
    expect(getGroupShortDisplayName("")).toBe("—");
    expect(getGroupShortDisplayName(undefined)).toBe("—");
  });
});

describe("formatTimeRangeET", () => {
  it("returns empty string for date-only or invalid start", () => {
    expect(formatTimeRangeET("2026-03-05", null)).toBe("");
    expect(formatTimeRangeET("", "2026-03-05T18:00:00Z")).toBe("");
    expect(formatTimeRangeET(undefined, null)).toBe("");
  });

  it("returns start time only when no end", () => {
    const result = formatTimeRangeET("2026-03-15T17:00:00.000Z", null);
    expect(result).toMatch(/\d{1,2}:\d{2}\s*(?:AM|PM)\s*ET/);
  });

  it("returns time range with en dash when both start and end", () => {
    const result = formatTimeRangeET(
      "2026-03-15T17:00:00.000Z",
      "2026-03-15T18:30:00.000Z"
    );
    expect(result).toContain(" ET");
    expect(result).toMatch(/–/); // en dash
  });
});

describe("formatDateOnly", () => {
  it("formats ISO string to YYYY-MM-DD", () => {
    expect(formatDateOnly("2026-03-05T17:00:00.000Z")).toBe("2026-03-05");
  });

  it("formats date-only string as-is", () => {
    expect(formatDateOnly("2026-03-05")).toBe("2026-03-05");
  });

  it("returns empty string for invalid or empty", () => {
    expect(formatDateOnly("")).toBe("");
    expect(formatDateOnly(undefined)).toBe("");
    expect(formatDateOnly("invalid")).toBe("");
  });
});

describe("cleanDisplayText", () => {
  it("removes [b], [/b], [i], [/i], [u], [/u] tags", () => {
    expect(cleanDisplayText("[b]bold[/b]")).toBe("bold");
    expect(cleanDisplayText("[i]italic[/i]")).toBe("italic");
    expect(cleanDisplayText("[u]underline[/u]")).toBe("underline");
    expect(cleanDisplayText("[b]Hello[/b] world")).toBe("Hello world");
    expect(cleanDisplayText("A [i]nested[/i] [b]mix[/b]")).toBe("A nested mix");
  });

  it("handles case-insensitive tags", () => {
    expect(cleanDisplayText("[B]bold[/B]")).toBe("bold");
    expect(cleanDisplayText("[I]italic[/I]")).toBe("italic");
    expect(cleanDisplayText("[U]underline[/U]")).toBe("underline");
  });

  it("collapses repeated whitespace", () => {
    expect(cleanDisplayText("a   b   c")).toBe("a b c");
    expect(cleanDisplayText("  multiple   spaces  ")).toBe("multiple spaces");
    expect(cleanDisplayText("tab\t\there")).toBe("tab here");
    expect(cleanDisplayText("new\n\nlines")).toBe("new lines");
  });

  it("trims leading and trailing whitespace", () => {
    expect(cleanDisplayText("  trimmed  ")).toBe("trimmed");
    expect(cleanDisplayText("\n\t  text  \t\n")).toBe("text");
  });

  it("combines tag removal with whitespace normalization", () => {
    expect(cleanDisplayText("[b]  Hello  [/b]  world")).toBe("Hello world");
    expect(cleanDisplayText("  [i]  nested  [/i]  ")).toBe("nested");
  });

  it("returns empty string for null, undefined, empty", () => {
    expect(cleanDisplayText("")).toBe("");
    expect(cleanDisplayText(null)).toBe("");
    expect(cleanDisplayText(undefined)).toBe("");
  });

  it("leaves plain text unchanged", () => {
    expect(cleanDisplayText("Plain text")).toBe("Plain text");
    expect(cleanDisplayText("No tags here")).toBe("No tags here");
  });
});

describe("formatMeetingDate", () => {
  it("formats date as long form", () => {
    expect(formatMeetingDate("2026-03-05")).toBe(
      "Thursday, March 05, 2026"
    );
  });

  it("returns — for empty or invalid", () => {
    expect(formatMeetingDate(undefined)).toBe("—");
    expect(formatMeetingDate("")).toBe("—");
    expect(formatMeetingDate("invalid")).toBe("—");
  });
});
