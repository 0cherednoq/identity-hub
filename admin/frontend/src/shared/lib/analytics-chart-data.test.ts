import { describe, expect, it } from "vitest";

import type { AnalyticsSeriesResponse } from "@/shared/api/generated/types.gen";
import { analyticsChartRows } from "./analytics-chart-data";

describe("analytics chart data", () => {
  it("preserves every long legend group and fills missing points with zero", () => {
    const groups = Array.from({ length: 12 }, (_, index) => ({
      key: `service-with-a-very-long-name-${String(index)}`,
      total: index,
      previous_total: 0,
      points:
        index === 0
          ? [{ at: "2026-01-01T00:00:00Z", value: index }]
          : [
              { at: "2026-01-01T00:00:00Z", value: index },
              { at: "2026-01-02T00:00:00Z", value: index + 1 },
            ],
    }));
    const series: AnalyticsSeriesResponse = {
      metric: "identities.added",
      bucket_size: "1d",
      group_by: "service",
      groups,
      coverage: "complete",
      cache_tag: "test",
    };

    const rows = analyticsChartRows(series, "Europe/Berlin");

    expect(rows).toHaveLength(2);
    expect(rows[1]?.group0).toBe(0);
    expect(rows[1]?.group11).toBe(12);
    expect(Object.keys(rows[0] ?? {})).toHaveLength(14);
  });
});
