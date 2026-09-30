import { adminV1ServiceGet, adminV1ServicesGet } from "./generated/sdk.gen";
import type {
  ResourceEnvelopeServiceCatalogResponse,
  ResourceEnvelopeServiceDescriptorResponse,
  ServiceCatalogResponse,
  ServiceDescriptorResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export async function fetchServices(
  signal?: AbortSignal,
): Promise<ServiceCatalogResponse> {
  const result = await adminV1ServicesGet({
    client,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeServiceCatalogResponse>(result, signal)
    .data;
}

export async function fetchServiceDetail(
  serviceName: string,
  signal?: AbortSignal,
): Promise<ServiceDescriptorResponse> {
  const result = await adminV1ServiceGet({
    client,
    path: { service_name: serviceName },
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeServiceDescriptorResponse>(
    result,
    signal,
  ).data;
}
