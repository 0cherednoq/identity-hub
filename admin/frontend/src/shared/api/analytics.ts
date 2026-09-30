import {
  adminV1AnalyticsBanRateGet,
  adminV1AnalyticsCatalogGet,
  adminV1AnalyticsCohortsGet,
  adminV1AnalyticsFunnelGet,
  adminV1AnalyticsProxyQualityGet,
  adminV1AnalyticsSeriesGet,
  adminV1AnalyticsTimeToGet,
  adminV1AnalyticsUtilizationGet,
} from "./generated/sdk.gen";
import type {
  AdminV1AnalyticsSeriesGetData,
  AnalyticsCatalogResponse,
  AnalyticsSeriesResponse,
  BanRateResponse,
  CohortResponse,
  FunnelResponse,
  ProxyQualityResponse,
  ResourceEnvelopeAnalyticsCatalogResponse,
  ResourceEnvelopeAnalyticsSeriesResponse,
  ResourceEnvelopeBanRateResponse,
  ResourceEnvelopeCohortResponse,
  ResourceEnvelopeFunnelResponse,
  ResourceEnvelopeProxyQualityResponse,
  ResourceEnvelopeTimeToResponse,
  ResourceEnvelopeUtilizationResponse,
  TimeToResponse,
  UtilizationResponse,
} from "./generated/types.gen";
import {
  AdminApiError,
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export type AnalyticsSeriesQuery = NonNullable<
  AdminV1AnalyticsSeriesGetData["query"]
>;

export interface AnalyticsDashboardQuery {
  start: string;
  end: string;
  bucket: AnalyticsSeriesQuery["bucket"];
  dimension?: string;
  service?: string;
}

export interface AnalyticsDashboardData {
  series: Record<string, AnalyticsSeriesResponse>;
  funnel: FunnelResponse;
  authTime: TimeToResponse;
  cohorts: CohortResponse;
  banRate: BanRateResponse;
  utilization: UtilizationResponse;
  proxyQuality: ProxyQualityResponse;
}

function data<Item>(envelope: { data: Item } | null): Item {
  if (envelope === null) throw new AdminApiError(304, null);
  return envelope.data;
}

export async function fetchAnalyticsCatalog(
  spaceSlug: string,
  signal?: AbortSignal,
): Promise<AnalyticsCatalogResponse> {
  const result = await adminV1AnalyticsCatalogGet({
    client,
    path: { space_slug: spaceSlug },
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeAnalyticsCatalogResponse>(
    result,
    signal,
  ).data;
}

function supportedDimension(
  catalog: AnalyticsCatalogResponse,
  metric: string,
  requested: string | undefined,
): string | undefined {
  if (requested === undefined || requested === "none") return undefined;
  return catalog.metrics
    .find((item) => item.metric === metric)
    ?.dimensions.includes(requested)
    ? requested
    : undefined;
}

export async function fetchAnalyticsDashboard(
  spaceSlug: string,
  catalog: AnalyticsCatalogResponse,
  query: AnalyticsDashboardQuery,
  signal?: AbortSignal,
): Promise<AnalyticsDashboardData> {
  const metrics = [
    "identities.added",
    "auth.authenticated",
    "verifications.verified",
    "tiers.reached",
    "standings.entered",
    "leases.acquired",
    "state.drift",
    "proxies.status",
  ] as const;
  const base = {
    start: query.start,
    end: query.end,
    ...(query.service === undefined ? {} : { service: query.service }),
  };
  const seriesRequests = metrics.map(async (metric) => {
    const groupBy = supportedDimension(catalog, metric, query.dimension);
    const result = await adminV1AnalyticsSeriesGet({
      client,
      path: { space_slug: spaceSlug },
      query: {
        ...base,
        metric,
        bucket: query.bucket,
        ...(groupBy === undefined ? {} : { group_by: groupBy }),
      },
      ...requestOptions(signal),
    });
    const envelope =
      unwrapApiResult<ResourceEnvelopeAnalyticsSeriesResponse | null>(
        result,
        signal,
      );
    return [metric, data(envelope)] as const;
  });
  const banGroup =
    query.dimension === "service" ||
    query.dimension === "tag" ||
    query.dimension === "proxy_pool"
      ? query.dimension
      : "batch";

  const [
    seriesEntries,
    funnelResult,
    authTimeResult,
    cohortsResult,
    banResult,
    utilizationResult,
    proxyResult,
  ] = await Promise.all([
    Promise.all(seriesRequests),
    adminV1AnalyticsFunnelGet({
      client,
      path: { space_slug: spaceSlug },
      query: base,
      ...requestOptions(signal),
    }),
    adminV1AnalyticsTimeToGet({
      client,
      path: { space_slug: spaceSlug },
      query: { ...base, metric: "added_to_authenticated" },
      ...requestOptions(signal),
    }),
    adminV1AnalyticsCohortsGet({
      client,
      path: { space_slug: spaceSlug },
      query: base,
      ...requestOptions(signal),
    }),
    adminV1AnalyticsBanRateGet({
      client,
      path: { space_slug: spaceSlug },
      query: { ...base, group_by: banGroup },
      ...requestOptions(signal),
    }),
    adminV1AnalyticsUtilizationGet({
      client,
      path: { space_slug: spaceSlug },
      query: base,
      ...requestOptions(signal),
    }),
    adminV1AnalyticsProxyQualityGet({
      client,
      path: { space_slug: spaceSlug },
      query: { start: query.start, end: query.end },
      ...requestOptions(signal),
    }),
  ]);

  return {
    series: Object.fromEntries(seriesEntries),
    funnel: data(
      unwrapApiResult<ResourceEnvelopeFunnelResponse | null>(
        funnelResult,
        signal,
      ),
    ),
    authTime: data(
      unwrapApiResult<ResourceEnvelopeTimeToResponse | null>(
        authTimeResult,
        signal,
      ),
    ),
    cohorts: data(
      unwrapApiResult<ResourceEnvelopeCohortResponse | null>(
        cohortsResult,
        signal,
      ),
    ),
    banRate: data(
      unwrapApiResult<ResourceEnvelopeBanRateResponse | null>(
        banResult,
        signal,
      ),
    ),
    utilization: data(
      unwrapApiResult<ResourceEnvelopeUtilizationResponse | null>(
        utilizationResult,
        signal,
      ),
    ),
    proxyQuality: data(
      unwrapApiResult<ResourceEnvelopeProxyQualityResponse | null>(
        proxyResult,
        signal,
      ),
    ),
  };
}
