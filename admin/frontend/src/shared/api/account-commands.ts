import {
  adminV1AccountChallengeAnswerPost,
  adminV1AccountLeaseRevokePost,
  adminV1AccountLinkPost,
  adminV1AccountLifecyclePost,
  adminV1AccountLoginPost,
  adminV1AccountProxyRebindPost,
  adminV1AccountRequirementResolvePost,
  adminV1AccountResyncPost,
  adminV1AccountStandingRestorePost,
  adminV1AccountStateOverridePost,
  adminV1AccountStateUnpinPost,
  adminV1AccountVerificationReviewPost,
  adminV1AccountUnlinkPost,
} from "./generated/sdk.gen";
import type {
  AccountActionResponse,
  AccountCommandResponse,
  ResourceCommandResponse,
  ResourceEnvelopeAccountCommandResponse,
  ResourceEnvelopeResourceCommandResponse,
} from "./generated/types.gen";
import { createAdminClient, unwrapApiResult } from "./transport";

const client = createAdminClient();

export interface AccountActionVariables {
  action: AccountActionResponse;
  answer?: string | undefined;
  identityId: string;
  pinSeconds?: number | undefined;
  reason: string;
  role?: string | undefined;
  revision: number;
  spaceSlug: string;
  target?: string | undefined;
}

function confirmation(action: AccountActionResponse) {
  return {
    confirmed: true as const,
    consequence_codes: action.consequence_codes ?? [],
  };
}

export type AccountCommandResult =
  AccountCommandResponse | ResourceCommandResponse;

export async function executeAccountAction(
  variables: AccountActionVariables,
  idempotencyKey: string,
): Promise<AccountCommandResult> {
  const common = {
    client,
    path: {
      space_slug: variables.spaceSlug,
      identity_id: variables.identityId,
    },
    headers: { "Idempotency-Key": idempotencyKey },
  };
  const mutation = {
    reason: variables.reason,
    expected_revision: variables.revision,
  };
  let result;
  switch (variables.action.kind) {
    case "lifecycle":
      if (
        variables.action.target === null ||
        variables.action.target === undefined
      ) {
        throw new Error("Lifecycle action has no target");
      }
      result = await adminV1AccountLifecyclePost({
        ...common,
        body: {
          ...mutation,
          target: variables.action.target as
            "provisioning" | "ready" | "retired",
          ...(variables.action.requires_confirmation
            ? { confirmation: confirmation(variables.action) }
            : {}),
        },
      });
      break;
    case "restore":
      result = await adminV1AccountStandingRestorePost({
        ...common,
        body: { ...mutation, confirmation: confirmation(variables.action) },
      });
      break;
    case "override":
      if (
        variables.action.region === null ||
        variables.action.region === undefined
      ) {
        throw new Error("Override action has no region");
      }
      if (variables.target === undefined)
        throw new Error("Override target is required");
      result = await adminV1AccountStateOverridePost({
        ...common,
        body: {
          ...mutation,
          region: variables.action.region as
            "lifecycle" | "standing" | "auth" | "availability",
          key: variables.action.key ?? "",
          target: variables.target,
          ...(variables.pinSeconds === undefined
            ? {}
            : { pin_seconds: variables.pinSeconds }),
          confirmation: confirmation(variables.action),
        },
      });
      break;
    case "unpin":
      if (
        variables.action.region !== "standing" &&
        variables.action.region !== "auth"
      ) {
        throw new Error("Unpin action has an unsupported region");
      }
      result = await adminV1AccountStateUnpinPost({
        ...common,
        body: {
          ...mutation,
          region: variables.action.region,
          key: variables.action.key ?? "",
          confirmation: confirmation(variables.action),
        },
      });
      break;
    case "resolve_requirement":
      result = await adminV1AccountRequirementResolvePost({
        ...common,
        body: { ...mutation, code: variables.action.key ?? "" },
      });
      break;
    case "run_login":
      result = await adminV1AccountLoginPost({
        ...common,
        body: mutation,
      });
      break;
    case "resync":
      result = await adminV1AccountResyncPost({
        ...common,
        body: { ...mutation, regions: [variables.action.key ?? "auth"] },
      });
      break;
    case "answer_challenge":
      if (variables.answer === undefined || variables.answer.length === 0)
        throw new Error("Challenge answer is required");
      result = await adminV1AccountChallengeAnswerPost({
        ...common,
        body: {
          ...mutation,
          challenge_id: variables.action.key ?? "",
          answer: variables.answer,
        },
      });
      break;
    case "verification_approve":
    case "verification_reject":
    case "verification_abandon":
      result = await adminV1AccountVerificationReviewPost({
        ...common,
        body: {
          ...mutation,
          factor: variables.action.key ?? "",
          action: variables.action.kind.replace("verification_", "") as
            "approve" | "reject" | "abandon",
          ...(variables.action.kind === "verification_reject"
            ? { code: variables.target ?? "" }
            : {}),
          confirmation: confirmation(variables.action),
        },
      });
      break;
    case "lease_revoke":
      result = await adminV1AccountLeaseRevokePost({
        ...common,
        body: { ...mutation, confirmation: confirmation(variables.action) },
      });
      break;
    case "proxy_rebind":
      if (variables.target === undefined || variables.target.length === 0)
        throw new Error("Target proxy is required");
      result = await adminV1AccountProxyRebindPost({
        ...common,
        body: {
          ...mutation,
          proxy_id: variables.target,
          confirmation: confirmation(variables.action),
        },
      });
      break;
    case "link":
      if (variables.role === undefined || variables.role.length === 0)
        throw new Error("Link role is required");
      if (variables.target === undefined || variables.target.length === 0)
        throw new Error("Linked account is required");
      result = await adminV1AccountLinkPost({
        ...common,
        body: {
          ...mutation,
          role: variables.role,
          linked_identity_id: variables.target,
        },
      });
      break;
    case "unlink":
      result = await adminV1AccountUnlinkPost({
        ...common,
        body: {
          ...mutation,
          role: variables.action.key ?? "",
          ...(variables.action.requires_confirmation
            ? { confirmation: confirmation(variables.action) }
            : {}),
        },
      });
      break;
  }
  return unwrapApiResult<
    | ResourceEnvelopeAccountCommandResponse
    | ResourceEnvelopeResourceCommandResponse
  >(result).data;
}
