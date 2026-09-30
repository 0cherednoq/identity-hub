import type { AnalyticsSeriesResponse } from "@/shared/api/generated/types.gen";

export interface AnalyticsChartRow {
  at: string;
  label: string;
  [key: string]: string | number;
}

export function analyticsChartRows(
  series: AnalyticsSeriesResponse,
  timezone: string,
): AnalyticsChartRow[] {
  const length = Math.max(
    0,
    ...series.groups.map((group) => group.points.length),
  );
  const formatter = new Intl.DateTimeFormat("ru-RU", {
    timeZone: timezone,
    month: "short",
    day: "2-digit",
    hour: series.bucket_size === "1d" ? undefined : "2-digit",
    minute: series.bucket_size === "1d" ? undefined : "2-digit",
  });
  return Array.from({ length }, (_, index) => {
    const point = series.groups[0]?.points[index];
    const at = point?.at ?? "";
    const row: AnalyticsChartRow = {
      at,
      label: at === "" ? "" : formatter.format(new Date(at)),
    };
    series.groups.forEach((group, groupIndex) => {
      row[`group${String(groupIndex)}`] = group.points[index]?.value ?? 0;
    });
    return row;
  });
}
