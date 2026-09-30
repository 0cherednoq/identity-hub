import { describe, expect, it } from "vitest";

import {
  mergeAnalyticsSearch,
  parseAnalyticsSearch,
  resolveAnalyticsRange,
} from "./analytics-search";

describe("analytics URL state", () => {
  it("rejects unknown enums and invalid dates", () => {
    expect(
      parseAnalyticsSearch({
        window: "forever",
        bucket: "5m",
        dimension: "identity_id",
        start: "not-a-date",
      }),
    ).toEqual({});
  });

  it("resolves deterministic presets and custom ranges", () => {
    const now = new Date("2026-01-31T12:00:00Z");
    expect(resolveAnalyticsRange({ window: "1d" }, now)).toEqual({
      start: "2026-01-30T12:00:00.000Z",
      end: "2026-01-31T12:00:00.000Z",
      bucket: "1h",
      window: "1d",
    });
    expect(
      resolveAnalyticsRange({
        window: "custom",
        start: "2026-01-01T00:00:00Z",
        end: "2026-01-02T00:00:00Z",
        bucket: "1m",
      }),
    ).toMatchObject({
      start: "2026-01-01T00:00:00.000Z",
      end: "2026-01-02T00:00:00.000Z",
      bucket: "1m",
      window: "custom",
    });
  });

  it("removes emptied filters", () => {
    expect(
      mergeAnalyticsSearch(
        { window: "30d", service: "demo" },
        { service: undefined },
      ),
    ).toEqual({ window: "30d" });
  });
});
