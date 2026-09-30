import {
  adminV1ImportCancelPost,
  adminV1ImportsGet,
  adminV1ImportStartPost,
} from "./generated/sdk.gen";
import type {
  ImportRunResponse,
  ImportRunsResponse,
  ImportStartCommandWritable,
  ResourceEnvelopeImportRunResponse,
  ResourceEnvelopeImportRunsResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export async function fetchImports(
  spaceSlug: string,
  signal?: AbortSignal,
): Promise<ImportRunsResponse> {
  const result = await adminV1ImportsGet({
    client,
    path: { space_slug: spaceSlug },
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeImportRunsResponse>(result, signal)
    .data;
}

export async function startImport(input: {
  command: ImportStartCommandWritable;
  spaceSlug: string;
}): Promise<ImportRunResponse> {
  const result = await adminV1ImportStartPost({
    client,
    path: { space_slug: input.spaceSlug },
    headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
    body: input.command,
  });
  return unwrapApiResult<ResourceEnvelopeImportRunResponse>(result).data;
}

export async function cancelImport(input: {
  reason: string;
  runId: string;
  spaceSlug: string;
}): Promise<ImportRunResponse> {
  const result = await adminV1ImportCancelPost({
    client,
    path: { space_slug: input.spaceSlug, run_id: input.runId },
    headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
    body: { reason: input.reason },
  });
  return unwrapApiResult<ResourceEnvelopeImportRunResponse>(result).data;
}
