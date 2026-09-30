import {
  adminV1OverviewGet,
  adminV1SpaceOverviewGet,
  adminV1SpacesGet,
} from "./generated/sdk.gen";
import type {
  ResourceEnvelopeSpaceOverviewResponse,
  ResourceEnvelopeSpacesOverviewResponse,
  SpaceOverviewResponse,
  SpacesOverviewResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

function query(service?: string) {
  return service === undefined ? {} : { service };
}

export async function fetchOverview(
  service?: string,
  signal?: AbortSignal,
): Promise<SpacesOverviewResponse> {
  const result = await adminV1OverviewGet({
    client,
    query: query(service),
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeSpacesOverviewResponse>(result, signal)
    .data;
}

export async function fetchSpaces(
  service?: string,
  signal?: AbortSignal,
): Promise<SpacesOverviewResponse> {
  const result = await adminV1SpacesGet({
    client,
    query: query(service),
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeSpacesOverviewResponse>(result, signal)
    .data;
}

export async function fetchSpaceOverview(
  spaceSlug: string,
  service?: string,
  signal?: AbortSignal,
): Promise<SpaceOverviewResponse> {
  const result = await adminV1SpaceOverviewGet({
    client,
    path: { space_slug: spaceSlug },
    query: query(service),
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeSpaceOverviewResponse>(result, signal)
    .data;
}
