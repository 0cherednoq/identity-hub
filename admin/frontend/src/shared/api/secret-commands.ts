import {
  adminV1AccountSecretRevealPost,
  adminV1AccountSecretUpdatePost,
} from "./generated/sdk.gen";
import type {
  SecretRevealResponse,
  SecretUpdateResponse,
  ResourceEnvelopeSecretRevealResponse,
  ResourceEnvelopeSecretUpdateResponse,
} from "./generated/types.gen";
import { createAdminClient, unwrapApiResult } from "./transport";

const client = createAdminClient();

interface SecretCommandBase {
  identityId: string;
  reason: string;
  reauthentication: string;
  revision: number;
  spaceSlug: string;
}

export interface RevealSecretVariables extends SecretCommandBase {
  field: string;
}

export interface UpdateSecretVariables extends SecretCommandBase {
  kind: "credentials" | "session";
  slot?: string | undefined;
  value: Record<string, unknown>;
}

export async function revealAccountSecret(
  variables: RevealSecretVariables,
): Promise<SecretRevealResponse> {
  const result = await adminV1AccountSecretRevealPost({
    client,
    path: {
      space_slug: variables.spaceSlug,
      identity_id: variables.identityId,
    },
    headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
    body: {
      reason: variables.reason,
      expected_revision: variables.revision,
      field: variables.field,
      reauthentication: variables.reauthentication,
      confirmation: {
        confirmed: true,
        consequence_codes: ["reveals_secret"],
      },
    },
  });
  return unwrapApiResult<ResourceEnvelopeSecretRevealResponse>(result).data;
}

export async function updateAccountSecret(
  variables: UpdateSecretVariables,
): Promise<SecretUpdateResponse> {
  const result = await adminV1AccountSecretUpdatePost({
    client,
    path: {
      space_slug: variables.spaceSlug,
      identity_id: variables.identityId,
    },
    headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
    body: {
      reason: variables.reason,
      expected_revision: variables.revision,
      kind: variables.kind,
      value: variables.value,
      ...(variables.kind === "session"
        ? { slot: variables.slot ?? "default" }
        : {}),
      reauthentication: variables.reauthentication,
      confirmation: {
        confirmed: true,
        consequence_codes: ["replaces_secret"],
      },
    },
  });
  return unwrapApiResult<ResourceEnvelopeSecretUpdateResponse>(result).data;
}
