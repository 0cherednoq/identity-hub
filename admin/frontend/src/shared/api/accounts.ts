import {
  adminV1AccountGet,
  adminV1AccountSectionGet,
  adminV1AccountsGet,
} from "./generated/sdk.gen";
import type {
  AccountHeaderResponse,
  AccountPageResponse,
  AccountSectionResponse,
  AdminV1AccountSectionGetData,
  AdminV1AccountsGetData,
  ResourceEnvelopeAccountHeaderResponse,
  ResourceEnvelopeAccountPageResponse,
  ResourceEnvelopeAccountSectionResponse,
} from "./generated/types.gen";
import {
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

const client = createAdminClient();

export type AccountQuery = NonNullable<AdminV1AccountsGetData["query"]>;

export async function fetchAccounts(
  spaceSlug: string,
  query: AccountQuery,
  signal?: AbortSignal,
): Promise<AccountPageResponse> {
  const result = await adminV1AccountsGet({
    client,
    path: { space_slug: spaceSlug },
    query,
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeAccountPageResponse>(result, signal)
    .data;
}

export type AccountSection = AdminV1AccountSectionGetData["path"]["section"];

export async function fetchAccount(
  spaceSlug: string,
  identityId: string,
  signal?: AbortSignal,
): Promise<AccountHeaderResponse> {
  const result = await adminV1AccountGet({
    client,
    path: { space_slug: spaceSlug, identity_id: identityId },
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeAccountHeaderResponse>(result, signal)
    .data;
}

export async function fetchAccountSection(
  spaceSlug: string,
  identityId: string,
  section: AccountSection,
  signal?: AbortSignal,
): Promise<AccountSectionResponse> {
  const result = await adminV1AccountSectionGet({
    client,
    path: { space_slug: spaceSlug, identity_id: identityId, section },
    ...requestOptions(signal),
  });
  return unwrapApiResult<ResourceEnvelopeAccountSectionResponse>(result, signal)
    .data;
}
