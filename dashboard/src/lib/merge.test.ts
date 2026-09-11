import { describe, it, expect } from "vitest";
import {
  mergeForCalendarItem,
  mergeUpcoming,
  mergeS3Resources,
  buildBubbleUrlSet,
  mergeAndDedupeMaterials,
  isLinkedById,
  isLinkedByDate,
  isS3CandidateForCalendar,
  getResourceUrl,
  getRelatedCalendarIds,
  getMeetingMetaDate,
  normalizeDate,
} from "./merge";
import type { CalendarItem, Resource } from "./bubble";
import type { BubbleReportResource } from "./s3";

// --- Sample payloads ---

const cal1: CalendarItem = {
  _id: "cal-1",
  title: "Life RBC WG",
  date: "2026-03-15",
};

const cal2: CalendarItem = {
  _id: "cal-2",
  title: "Capital Adequacy TF",
  date: "2026-03-18",
};

const bubbleRes1: Resource = {
  _id: "res-b1",
  Name: "Agenda PDF",
  URL: "https://example.com/agenda.pdf",
  "Related calendar items": ["cal-1"],
};

const bubbleRes2: Resource = {
  _id: "res-b2",
  Name: "Materials",
  URL: "https://example.com/materials.pdf",
  "Related calendar items": ["cal-1"],
};

const s3ResLinkedById: BubbleReportResource = {
  Name: "New Doc",
  URL: "https://example.com/new.pdf",
  "Related calendar items": ["cal-1"],
};

const s3ResLinkedByDate: BubbleReportResource = {
  Name: "Date-matched Doc",
  URL: "https://example.com/date-match.pdf",
  __meeting_meta: { date_iso: "2026-03-15" },
};

const s3ResDuplicateUrl: BubbleReportResource = {
  Name: "S3 version of agenda",
  URL: "https://example.com/agenda.pdf",
  "Related calendar items": ["cal-1"],
};

const s3ResNotLinked: BubbleReportResource = {
  Name: "Unrelated",
  URL: "https://example.com/unrelated.pdf",
  "Related calendar items": ["cal-2"],
};

const s3ResNoUrl: BubbleReportResource = {
  Name: "No URL",
  "Related calendar items": ["cal-1"],
};

describe("mergeAndDedupeMaterials", () => {
  it("merges agenda and linked materials; final length is 3 when 1 agenda + 2 linked with no URL overlap", () => {
    const agendaMaterials = [
      {
        name: "Agenda Doc",
        url: "https://example.com/agenda.pdf",
        source: "bubble" as const,
        missingInBubble: false,
        isAgenda: true,
      },
    ];
    const linkedMaterials = [
      {
        name: "Materials",
        url: "https://example.com/materials.pdf",
        source: "bubble" as const,
        missingInBubble: false,
        isAgenda: false,
      },
      {
        name: "S3 Doc",
        url: "https://example.com/s3-doc.pdf",
        source: "s3" as const,
        missingInBubble: true,
        isAgenda: false,
      },
    ];
    const result = mergeAndDedupeMaterials(agendaMaterials, linkedMaterials);
    expect(result).toHaveLength(3);
    expect(result[0].name).toBe("Agenda Doc");
    expect(result[0].url).toBe("https://example.com/agenda.pdf");
    expect(result[1].name).toBe("Materials");
    expect(result[2].name).toBe("S3 Doc");
  });

  it("dedupes by URL when overlap; keeps first occurrence (agenda wins)", () => {
    const agendaMaterials = [
      {
        name: "Agenda",
        url: "https://example.com/same.pdf",
        source: "bubble" as const,
        missingInBubble: false,
        isAgenda: true,
      },
    ];
    const linkedMaterials = [
      {
        name: "Linked Same",
        url: "https://example.com/same.pdf",
        source: "bubble" as const,
        missingInBubble: false,
        isAgenda: false,
      },
    ];
    const result = mergeAndDedupeMaterials(agendaMaterials, linkedMaterials);
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe("Agenda");
  });
});

describe("merge helpers", () => {
  it("getResourceUrl extracts URL", () => {
    expect(getResourceUrl({ URL: "https://a.com" })).toBe("https://a.com");
    expect(getResourceUrl({ url: "https://b.com" })).toBe("https://b.com");
    expect(getResourceUrl({})).toBe("");
  });

  it("getRelatedCalendarIds parses array, string, and object entries", () => {
    expect(getRelatedCalendarIds({ "Related calendar items": ["cal-1", "cal-2"] })).toEqual([
      "cal-1",
      "cal-2",
    ]);
    expect(getRelatedCalendarIds({ "Related calendar items": "cal-1" })).toEqual(["cal-1"]);
    expect(
      getRelatedCalendarIds({
        "Related calendar items": [{ _id: "cal-1" }, { id: "cal-2" }],
      })
    ).toEqual(["cal-1", "cal-2"]);
    expect(getRelatedCalendarIds({})).toEqual([]);
  });

  it("getMeetingMetaDate extracts date_iso", () => {
    expect(getMeetingMetaDate({ __meeting_meta: { date_iso: "2026-03-15" } })).toBe(
      "2026-03-15"
    );
    expect(getMeetingMetaDate({})).toBeUndefined();
  });

  it("normalizeDate trims to YYYY-MM-DD", () => {
    expect(normalizeDate("2026-03-15")).toBe("2026-03-15");
    expect(normalizeDate("2026-03-15T14:00:00Z")).toBe("2026-03-15");
    expect(normalizeDate(undefined)).toBeUndefined();
  });

  it("isLinkedById checks Related calendar items", () => {
    expect(isLinkedById(s3ResLinkedById, "cal-1")).toBe(true);
    expect(isLinkedById(s3ResLinkedById, "cal-2")).toBe(false);
    expect(isLinkedById(s3ResNotLinked, "cal-1")).toBe(false);
  });

  it("isLinkedByDate checks __meeting_meta.date_iso", () => {
    expect(isLinkedByDate(s3ResLinkedByDate, "2026-03-15")).toBe(true);
    expect(isLinkedByDate(s3ResLinkedByDate, "2026-03-18")).toBe(false);
    expect(isLinkedByDate(s3ResLinkedById, undefined)).toBe(false);
  });

  it("isS3CandidateForCalendar combines primary and secondary", () => {
    expect(isS3CandidateForCalendar(s3ResLinkedById, "cal-1", "2026-03-15")).toBe(true);
    expect(isS3CandidateForCalendar(s3ResLinkedByDate, "cal-1", "2026-03-15")).toBe(true);
    expect(isS3CandidateForCalendar(s3ResNotLinked, "cal-1", "2026-03-15")).toBe(false);
  });
});

describe("buildBubbleUrlSet", () => {
  it("collects URLs from all calendar items", () => {
    const byCal: Record<string, Resource[]> = {
      "cal-1": [bubbleRes1, bubbleRes2],
      "cal-2": [],
    };
    const urls = buildBubbleUrlSet(byCal);
    expect(urls.size).toBe(2);
    expect(urls.has("https://example.com/agenda.pdf")).toBe(true);
    expect(urls.has("https://example.com/materials.pdf")).toBe(true);
  });
});

describe("mergeS3Resources", () => {
  it("dedupes by URL, latest first", () => {
    const latest = [s3ResLinkedById, s3ResLinkedByDate];
    const recent = [
      [
        { ...s3ResLinkedById, Name: "Updated" },
        { Name: "Extra", URL: "https://example.com/extra.pdf" },
      ],
    ];
    const merged = mergeS3Resources(latest, recent);
    expect(merged.length).toBe(3);
    const byUrl = new Map(merged.map((r) => [getResourceUrl(r), r]));
    expect(byUrl.get("https://example.com/new.pdf")?.Name).toBe("New Doc");
    expect(byUrl.get("https://example.com/date-match.pdf")).toBeDefined();
    expect(byUrl.get("https://example.com/extra.pdf")?.Name).toBe("Extra");
    expect(byUrl.get("https://example.com/new.pdf")).toBe(s3ResLinkedById);
  });
});

describe("mergeForCalendarItem", () => {
  it("returns bubble resources and s3 candidates, excludes duplicates", () => {
    const bubbleUrls = new Set([
      "https://example.com/agenda.pdf",
      "https://example.com/materials.pdf",
    ]);
    const s3CandidatesForCal1 = [
      s3ResLinkedById,
      s3ResLinkedByDate,
      s3ResDuplicateUrl,
    ];

    const vm = mergeForCalendarItem(
      cal1,
      [bubbleRes1, bubbleRes2],
      s3CandidatesForCal1,
      bubbleUrls
    );

    expect(vm.calendarItem).toBe(cal1);
    expect(vm.resources.bubble).toHaveLength(2);
    expect(vm.resources.s3Candidates).toHaveLength(2);
    expect(vm.resources.s3Candidates.map((r) => getResourceUrl(r))).toEqual([
      "https://example.com/new.pdf",
      "https://example.com/date-match.pdf",
    ]);
    expect(vm.flags.missingInBubble).toHaveLength(2);
    expect(vm.flags.counts).toEqual({
      bubble: 2,
      s3Candidates: 2,
      missingInBubble: 2,
    });
  });

  it("flags s3 candidates whose URL is in Bubble", () => {
    const bubbleUrls = new Set(["https://example.com/new.pdf"]);
    const vm = mergeForCalendarItem(
      cal1,
      [],
      [s3ResLinkedById, s3ResLinkedByDate],
      bubbleUrls
    );
    expect(vm.flags.missingInBubble).toHaveLength(1);
    expect(getResourceUrl(vm.flags.missingInBubble[0])).toBe(
      "https://example.com/date-match.pdf"
    );
  });

  it("skips S3 resources without URL", () => {
    const vm = mergeForCalendarItem(
      cal1,
      [],
      [s3ResNoUrl, s3ResLinkedById],
      new Set()
    );
    expect(vm.resources.s3Candidates).toHaveLength(1);
    expect(getResourceUrl(vm.resources.s3Candidates[0])).toBe(
      "https://example.com/new.pdf"
    );
  });
});

describe("mergeUpcoming", () => {
  it("produces deterministic UpcomingViewModel[]", () => {
    const calendarItems: CalendarItem[] = [cal1, cal2];
    const bubbleByCal: Record<string, Resource[]> = {
      "cal-1": [bubbleRes1],
      "cal-2": [],
    };
    const s3Resources: BubbleReportResource[] = [
      s3ResLinkedById,
      s3ResLinkedByDate,
      s3ResNotLinked,
    ];

    const result = mergeUpcoming(calendarItems, bubbleByCal, s3Resources);

    expect(result).toHaveLength(2);

    const vm1 = result[0];
    expect(vm1.calendarItem._id).toBe("cal-1");
    expect(vm1.resources.bubble).toHaveLength(1);
    expect(vm1.resources.s3Candidates).toHaveLength(2);
    expect(vm1.flags.counts.bubble).toBe(1);
    expect(vm1.flags.counts.s3Candidates).toBe(2);

    const vm2 = result[1];
    expect(vm2.calendarItem._id).toBe("cal-2");
    expect(vm2.resources.bubble).toHaveLength(0);
    expect(vm2.resources.s3Candidates).toHaveLength(1);
    expect(getResourceUrl(vm2.resources.s3Candidates[0])).toBe(
      "https://example.com/unrelated.pdf"
    );
  });

  it("same inputs produce same output (deterministic)", () => {
    const calendarItems: CalendarItem[] = [cal1];
    const bubbleByCal: Record<string, Resource[]> = {
      "cal-1": [bubbleRes1],
    };
    const s3Resources: BubbleReportResource[] = [s3ResLinkedById];

    const a = mergeUpcoming(calendarItems, bubbleByCal, s3Resources);
    const b = mergeUpcoming(calendarItems, bubbleByCal, s3Resources);

    expect(JSON.stringify(a)).toBe(JSON.stringify(b));
  });
});
