import { adminV1DeliveryCenterGet } from "./generated/sdk.gen";
import type {
  AdminV1DeliveryCenterGetData,
  DeliveryCenterResponse,
  ResourceEnvelopeDeliveryCenterResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export type DeliveryCenterQuery = NonNullable<
  AdminV1DeliveryCenterGetData["query"]
>;

export async function fetchDeliveryCenter(
  spaceSlug: string,
  query: DeliveryCenterQuery,
  signal?: AbortSignal,
): Promise<DeliveryCenterResponse> {
  const result = await adminV1DeliveryCenterGet({
    client,
    path: { space_slug: spaceSlug },
    query,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeDeliveryCenterResponse>(result, signal)
    .data;
}
