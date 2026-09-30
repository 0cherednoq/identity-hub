export const ANALYTICS_WINDOWS = ["1d", "7d", "30d", "custom"] as const;
export const ANALYTICS_BUCKETS = ["1m", "1h", "1d"] as const;
export const ANALYTICS_DIMENSIONS = [
  "none",
  "service",
  "method",
  "factor",
  "state",
  "code",
  "kind",
  "pool",
  "batch",
  "tag",
  "proxy_pool",
] as const;

export type AnalyticsWindow = (typeof ANALYTICS_WINDOWS)[number];
export type AnalyticsBucket = (typeof ANALYTICS_BUCKETS)[number];
export type AnalyticsDimension = (typeof ANALYTICS_DIMENSIONS)[number];

export interface AnalyticsSearch {
  window?: AnalyticsWindow;
  bucket?: AnalyticsBucket;
  dimension?: AnalyticsDimension;
  service?: string;
  start?: string;
  end?: string;
}

export interface AnalyticsRange {
  start: string;
  end: string;
  bucket: AnalyticsBucket;
  window: AnalyticsWindow;
}

function includes<T extends string>(
  values: readonly T[],
  value: unknown,
): value is T {
  return typeof value === "string" && values.includes(value as T);
}

function validDate(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

export function parseAnalyticsSearch(
  search: Record<string, unknown>,
): AnalyticsSearch {
  const window = includes(ANALYTICS_WINDOWS, search.window)
    ? search.window
    : undefined;
  const bucket = includes(ANALYTICS_BUCKETS, search.bucket)
    ? search.bucket
    : undefined;
  const dimension = includes(ANALYTICS_DIMENSIONS, search.dimension)
    ? search.dimension
    : undefined;
  const service =
    typeof search.service === "string" && search.service !== ""
      ? search.service.slice(0, 64)
      : undefined;
  const start = validDate(search.start) ? search.start : undefined;
  const end = validDate(search.end) ? search.end : undefined;
  return {
    ...(window === undefined ? {} : { window }),
    ...(bucket === undefined ? {} : { bucket }),
    ...(dimension === undefined ? {} : { dimension }),
    ...(service === undefined ? {} : { service }),
    ...(start === undefined ? {} : { start }),
    ...(end === undefined ? {} : { end }),
  };
}

export function resolveAnalyticsRange(
  search: AnalyticsSearch,
  now = new Date(),
): AnalyticsRange {
  const window = search.window ?? "7d";
  if (
    window === "custom" &&
    search.start !== undefined &&
    search.end !== undefined &&
    Date.parse(search.start) < Date.parse(search.end)
  ) {
    return {
      start: new Date(search.start).toISOString(),
      end: new Date(search.end).toISOString(),
      bucket: search.bucket ?? "1d",
      window,
    };
  }
  const days = window === "1d" ? 1 : window === "30d" ? 30 : 7;
  return {
    start: new Date(now.getTime() - days * 86_400_000).toISOString(),
    end: now.toISOString(),
    bucket: search.bucket ?? (window === "1d" ? "1h" : "1d"),
    window: window === "custom" ? "7d" : window,
  };
}

export type AnalyticsSearchPatch = Partial<
  Record<keyof AnalyticsSearch, string | undefined>
>;

export function mergeAnalyticsSearch(
  current: AnalyticsSearch,
  patch: AnalyticsSearchPatch,
): AnalyticsSearch {
  return parseAnalyticsSearch({ ...current, ...patch });
}
