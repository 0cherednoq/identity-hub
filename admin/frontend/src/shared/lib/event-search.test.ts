import { describe, expect, it } from "vitest";

import { mergeEventSearch, parseEventSearch } from "./event-search";

describe("event journal URL state", () => {
  it("keeps supported filters and rejects invalid enums", () => {
    expect(
      parseEventSearch({
        group: "identity",
        includeJournal: "true",
        severity: "warning",
        selected: "event-id",
        unknown: "ignored",
      }),
    ).toEqual({
      group: "identity",
      includeJournal: true,
      severity: "warning",
      selected: "event-id",
    });
    expect(parseEventSearch({ severity: "urgent" })).toEqual({});
  });

  it("resets pagination for filters but keeps it for the drawer", () => {
    const previous = { cursor: "next", group: "identity" };
    expect(mergeEventSearch(previous, { severity: "critical" })).toEqual({
      group: "identity",
      severity: "critical",
    });
    expect(mergeEventSearch(previous, { selected: "event-id" })).toEqual({
      cursor: "next",
      group: "identity",
      selected: "event-id",
    });
  });
});
