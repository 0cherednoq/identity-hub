import { adminV1LeasesGet, adminV1LeasesHistoryGet } from "./generated/sdk.gen";
import type {
  AdminV1LeasesGetData,
  AdminV1LeasesHistoryGetData,
  LeaseDashboardResponse,
  LeaseHistoryPageResponse,
  ResourceEnvelopeLeaseDashboardResponse,
  ResourceEnvelopeLeaseHistoryPageResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export type LeaseDashboardQuery = NonNullable<AdminV1LeasesGetData["query"]>;
export type LeaseHistoryQuery = NonNullable<
  AdminV1LeasesHistoryGetData["query"]
>;

export async function fetchLeaseDashboard(
  spaceSlug: string,
  query: LeaseDashboardQuery,
  signal?: AbortSignal,
): Promise<LeaseDashboardResponse> {
  const result = await adminV1LeasesGet({
    client,
    path: { space_slug: spaceSlug },
    query,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeLeaseDashboardResponse>(result, signal)
    .data;
}

export async function fetchLeaseHistory(
  spaceSlug: string,
  query: LeaseHistoryQuery,
  signal?: AbortSignal,
): Promise<LeaseHistoryPageResponse> {
  const result = await adminV1LeasesHistoryGet({
    client,
    path: { space_slug: spaceSlug },
    query,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeLeaseHistoryPageResponse>(
    result,
    signal,
  ).data;
}
