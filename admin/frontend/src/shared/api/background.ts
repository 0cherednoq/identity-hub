import { adminV1BackgroundGet } from "./generated/sdk.gen";
import type {
  BackgroundSnapshotResponse,
  ResourceEnvelopeBackgroundSnapshotResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export async function fetchBackground(
  signal?: AbortSignal,
): Promise<BackgroundSnapshotResponse> {
  const result = await adminV1BackgroundGet({
    client,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeBackgroundSnapshotResponse>(
    result,
    signal,
  ).data;
}
