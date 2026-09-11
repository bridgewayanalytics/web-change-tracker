import { describe, it, expect } from "vitest";
import {
  isAgenda,
  classifyResourcesForCalendarItem,
} from "./calendar-resources";

describe("isAgenda", () => {
  it("returns true when Name contains agenda (case-insensitive)", () => {
    expect(isAgenda({ Name: "Meeting Agenda" })).toBe(true);
    expect(isAgenda({ Name: "AGENDA 2026" })).toBe(true);
    expect(isAgenda({ Name: "pre-agenda notes" })).toBe(true);
  });

  it("returns true when Name starts with Agenda", () => {
    expect(isAgenda({ Name: "Agenda for March" })).toBe(true);
    expect(isAgenda({ Name: "Agenda" })).toBe(true);
  });

  it("returns true when URL contains agenda", () => {
    expect(isAgenda({ URL: "https://example.com/agenda.pdf" })).toBe(true);
    expect(isAgenda({ URL: "https://example.com/meeting/AGENDA-2026.pdf" })).toBe(true);
  });

  it("returns false for non-agenda resources", () => {
    expect(isAgenda({ Name: "Materials", URL: "https://example.com/materials.pdf" })).toBe(false);
    expect(isAgenda({ Name: "Presentation" })).toBe(false);
  });
});

describe("classifyResourcesForCalendarItem", () => {
  it("splits agendas and related", () => {
    const bubble = [
      { Name: "Agenda", URL: "https://example.com/agenda.pdf" },
      { Name: "Materials", URL: "https://example.com/materials.pdf" },
    ];
    const s3: typeof bubble = [];
    const missing = new Set<string>();
    const { agendas, related } = classifyResourcesForCalendarItem(
      bubble,
      s3,
      missing
    );
    expect(agendas).toHaveLength(1);
    expect(agendas[0].name).toBe("agenda");
    expect(agendas[0].url).toBe("https://example.com/agenda.pdf");
    expect(agendas[0].source).toBe("bubble");
    expect(agendas[0].missingInBubble).toBe(false);

    expect(related).toHaveLength(1);
    expect(related[0].name).toBe("materials");
  });

  it("dedupes by URL, Bubble wins", () => {
    const bubble = [
      { Name: "Agenda (Bubble)", URL: "https://example.com/agenda.pdf" },
    ];
    const s3 = [
      { Name: "Agenda (S3)", URL: "https://example.com/agenda.pdf" },
    ];
    const { agendas } = classifyResourcesForCalendarItem(
      bubble,
      s3,
      new Set()
    );
    expect(agendas).toHaveLength(1);
    expect(agendas[0].name).toBe("Agenda (Bubble)");
    expect(agendas[0].source).toBe("bubble");
  });

  it("marks S3-only resources as missingInBubble", () => {
    const bubble: { Name?: string; URL?: string }[] = [];
    const s3 = [
      { Name: "New Doc", URL: "https://example.com/new.pdf" },
    ];
    const missing = new Set(["https://example.com/new.pdf"]);
    const { related } = classifyResourcesForCalendarItem(bubble, s3, missing);
    expect(related).toHaveLength(1);
    expect(related[0].missingInBubble).toBe(true);
  });

  it("uses URL basename when Name is empty", () => {
    const bubble = [
      { URL: "https://example.com/path/to/file.pdf" },
    ];
    const { related } = classifyResourcesForCalendarItem(
      bubble,
      [],
      new Set()
    );
    expect(related).toHaveLength(1);
    expect(related[0].name).toBe("file");
  });

  it("returns empty arrays when no resources", () => {
    const { agendas, related } = classifyResourcesForCalendarItem(
      [],
      [],
      new Set()
    );
    expect(agendas).toHaveLength(0);
    expect(related).toHaveLength(0);
  });
});
