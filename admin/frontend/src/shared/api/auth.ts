import {
  adminV1AuthLoginPost,
  adminV1AuthLogoutPost,
  adminV1AuthMeGet,
} from "./generated/sdk.gen";
import type {
  PrincipalResponse,
  ResourceEnvelopeLogoutResponse,
  ResourceEnvelopePrincipalResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export async function fetchPrincipal(
  signal?: AbortSignal,
): Promise<PrincipalResponse> {
  const result = await adminV1AuthMeGet({ client, ...requestOptions(signal) });
  return unwrapApiResult<ResourceEnvelopePrincipalResponse>(result, signal)
    .data;
}

export async function login(
  credentials: { username: string; password: string },
  signal?: AbortSignal,
): Promise<PrincipalResponse> {
  const result = await adminV1AuthLoginPost({
    body: credentials,
    client,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopePrincipalResponse>(result, signal)
    .data;
}

export async function logout(signal?: AbortSignal): Promise<void> {
  const result = await adminV1AuthLogoutPost({
    client,
    ...requestOptions(signal),
  });
  unwrapApiResult<ResourceEnvelopeLogoutResponse>(result, signal);
}
