import { adminV1HumanQueueGet } from "./generated/sdk.gen";
import type {
  AdminV1HumanQueueGetData,
  HumanQueuePageResponse,
  ResourceEnvelopeHumanQueuePageResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export type HumanQueueQuery = NonNullable<AdminV1HumanQueueGetData["query"]>;

export async function fetchHumanQueue(
  spaceSlug: string,
  query: HumanQueueQuery,
  signal?: AbortSignal,
): Promise<HumanQueuePageResponse> {
  const result = await adminV1HumanQueueGet({
    client,
    path: { space_slug: spaceSlug },
    query,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeHumanQueuePageResponse>(result, signal)
    .data;
}
