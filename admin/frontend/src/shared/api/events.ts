import { adminV1EventGet, adminV1EventsGet } from "./generated/sdk.gen";
import type {
  AdminV1EventsGetData,
  EventItemResponse,
  EventPageResponse,
  ResourceEnvelopeEventItemResponse,
  ResourceEnvelopeEventPageResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export type EventListQuery = NonNullable<AdminV1EventsGetData["query"]>;

export async function fetchEvents(
  spaceSlug: string,
  query: EventListQuery,
  signal?: AbortSignal,
): Promise<EventPageResponse> {
  const result = await adminV1EventsGet({
    client,
    path: { space_slug: spaceSlug },
    query,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeEventPageResponse>(result, signal)
    .data;
}

export async function fetchEventDetail(
  spaceSlug: string,
  eventId: string,
  signal?: AbortSignal,
): Promise<EventItemResponse> {
  const result = await adminV1EventGet({
    client,
    path: { space_slug: spaceSlug, event_id: eventId },
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeEventItemResponse>(result, signal)
    .data;
}
