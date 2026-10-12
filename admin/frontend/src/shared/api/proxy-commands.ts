import {
  adminV1ProxyDisablePost,
  adminV1ProxyEnablePost,
  adminV1ProxyOverridePost,
  adminV1ProxyRecheckPost,
} from "./generated/sdk.gen";
import type {
  ProxyActionResponse,
  ResourceCommandResponse,
  ResourceEnvelopeResourceCommandResponse,
} from "./generated/types.gen";
import { createAdminClient, unwrapApiResult } from "./transport";

const client = createAdminClient();

export interface ProxyActionVariables {
  action: ProxyActionResponse;
  pinSeconds?: number | undefined;
  proxyId: string;
  reason: string;
  spaceSlug: string;
  target?: string | undefined;
}

export async function executeProxyAction(
  variables: ProxyActionVariables,
  idempotencyKey: string,
): Promise<ResourceCommandResponse> {
  const common = {
    client,
    path: { space_slug: variables.spaceSlug, proxy_id: variables.proxyId },
    headers: { "Idempotency-Key": idempotencyKey },
  };
  const body = { reason: variables.reason };
  const confirmation = {
    confirmed: true as const,
    consequence_codes: variables.action.consequence_codes ?? [],
  };
  if (variables.action.kind === "disable") {
    const disabled = await adminV1ProxyDisablePost({
      ...common,
      body: { ...body, confirmation },
    });
    return unwrapApiResult<ResourceEnvelopeResourceCommandResponse>(disabled)
      .data;
  }
  if (variables.action.kind === "enable") {
    const enabled = await adminV1ProxyEnablePost({ ...common, body });
    return unwrapApiResult<ResourceEnvelopeResourceCommandResponse>(enabled)
      .data;
  }
  const result =
    variables.action.kind === "recheck"
      ? await adminV1ProxyRecheckPost({ ...common, body })
      : await adminV1ProxyOverridePost({
          ...common,
          body: {
            ...body,
            status: variables.target as "alive" | "quarantined" | "dead",
            ...(variables.pinSeconds === undefined
              ? {}
              : { pin_seconds: variables.pinSeconds }),
            confirmation,
          },
        });
  return unwrapApiResult<ResourceEnvelopeResourceCommandResponse>(result).data;
}
