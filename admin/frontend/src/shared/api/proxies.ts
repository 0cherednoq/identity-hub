import { adminV1ProxiesGet, adminV1ProxyGet } from "./generated/sdk.gen";
import type {
  AdminV1ProxiesGetData,
  AdminV1ProxyGetData,
  ProxyDetailResponse,
  ProxyPageResponse,
  ResourceEnvelopeProxyDetailResponse,
  ResourceEnvelopeProxyPageResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export type ProxyListQuery = NonNullable<AdminV1ProxiesGetData["query"]>;
export type ProxyDetailQuery = NonNullable<AdminV1ProxyGetData["query"]>;

export async function fetchProxies(
  spaceSlug: string,
  query: ProxyListQuery,
  signal?: AbortSignal,
): Promise<ProxyPageResponse> {
  const result = await adminV1ProxiesGet({
    client,
    path: { space_slug: spaceSlug },
    query,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeProxyPageResponse>(result, signal)
    .data;
}

export async function fetchProxyDetail(
  spaceSlug: string,
  proxyId: string,
  query: ProxyDetailQuery,
  signal?: AbortSignal,
): Promise<ProxyDetailResponse> {
  const result = await adminV1ProxyGet({
    client,
    path: { space_slug: spaceSlug, proxy_id: proxyId },
    query,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeProxyDetailResponse>(result, signal)
    .data;
}
