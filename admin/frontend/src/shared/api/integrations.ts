import {
  adminV1NotificationChannelCreatePost,
  adminV1NotificationChannelDeletePost,
  adminV1NotificationChannelEnablePost,
  adminV1NotificationChannelTestPost,
  adminV1NotificationDeliveryRetryPost,
  adminV1NotificationRuleCreatePost,
  adminV1NotificationRuleDeletePost,
  adminV1NotificationRuleEnablePost,
  adminV1WebhookDeliveryRetryPost,
  adminV1WebhookEndpointCreatePost,
  adminV1WebhookEndpointDeletePost,
  adminV1WebhookEndpointEnablePost,
  adminV1WebhookEndpointRotatePost,
  adminV1WebhookEndpointTestPost,
} from "./generated/sdk.gen";
import type {
  ChannelCreateCommandWritable,
  IntegrationCommandResponse,
  OneTimeSigningValueResponse,
  ResourceEnvelopeIntegrationCommandResponse,
  ResourceEnvelopeOneTimeSigningValueResponse,
  ResourceEnvelopeWebhookTestResponse,
  RuleCreateCommand,
  WebhookCreateCommand,
  WebhookTestResponse,
} from "./generated/types.gen";
import { createAdminClient, unwrapApiResult } from "./transport";

const client = createAdminClient();

type Common = { reason: string; spaceSlug: string };

export type IntegrationCommand =
  | (Common & {
      body: Omit<ChannelCreateCommandWritable, "reason">;
      kind: "channel.create";
    })
  | (Common & {
      channelId: string;
      enabled: boolean;
      kind: "channel.enable";
    })
  | (Common & { channelId: string; kind: "channel.test"; text: string })
  | (Common & { channelId: string; kind: "channel.delete" })
  | (Common & {
      body: Omit<RuleCreateCommand, "reason">;
      kind: "rule.create";
    })
  | (Common & { enabled: boolean; kind: "rule.enable"; ruleId: string })
  | (Common & { kind: "rule.delete"; ruleId: string })
  | (Common & {
      body: Omit<WebhookCreateCommand, "reason">;
      kind: "webhook.create";
    })
  | (Common & { enabled: boolean; endpointId: string; kind: "webhook.enable" })
  | (Common & {
      endpointId: string;
      kind: "webhook.rotate";
      overlapSeconds: number;
    })
  | (Common & { endpointId: string; kind: "webhook.test" })
  | (Common & { endpointId: string; kind: "webhook.delete" })
  | (Common & {
      deliveryId: number;
      kind: "notification-delivery.retry";
    })
  | (Common & { deliveryId: number; kind: "delivery.retry" });

export type IntegrationCommandResult =
  | IntegrationCommandResponse
  | OneTimeSigningValueResponse
  | WebhookTestResponse;

const confirmation = (code: string) => ({
  confirmed: true as const,
  consequence_codes: [code],
});

export async function executeIntegrationCommand(
  command: IntegrationCommand,
  idempotencyKey: string,
): Promise<IntegrationCommandResult> {
  const common = {
    client,
    headers: { "Idempotency-Key": idempotencyKey },
    path: { space_slug: command.spaceSlug },
  };
  switch (command.kind) {
    case "channel.create": {
      const result = await adminV1NotificationChannelCreatePost({
        ...common,
        body: { ...command.body, reason: command.reason },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "channel.enable": {
      const result = await adminV1NotificationChannelEnablePost({
        ...common,
        path: { ...common.path, channel_id: command.channelId },
        body: { enabled: command.enabled, reason: command.reason },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "channel.test": {
      const result = await adminV1NotificationChannelTestPost({
        ...common,
        path: { ...common.path, channel_id: command.channelId },
        body: { reason: command.reason, text: command.text },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "channel.delete": {
      const result = await adminV1NotificationChannelDeletePost({
        ...common,
        path: { ...common.path, channel_id: command.channelId },
        body: {
          confirmation: confirmation("deletes_integration"),
          reason: command.reason,
        },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "rule.create": {
      const result = await adminV1NotificationRuleCreatePost({
        ...common,
        body: { ...command.body, reason: command.reason },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "rule.enable": {
      const result = await adminV1NotificationRuleEnablePost({
        ...common,
        path: { ...common.path, rule_id: command.ruleId },
        body: { enabled: command.enabled, reason: command.reason },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "rule.delete": {
      const result = await adminV1NotificationRuleDeletePost({
        ...common,
        path: { ...common.path, rule_id: command.ruleId },
        body: {
          confirmation: confirmation("deletes_integration"),
          reason: command.reason,
        },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "webhook.create": {
      const result = await adminV1WebhookEndpointCreatePost({
        ...common,
        body: { ...command.body, reason: command.reason },
      });
      return unwrapApiResult<ResourceEnvelopeOneTimeSigningValueResponse>(
        result,
      ).data;
    }
    case "webhook.enable": {
      const result = await adminV1WebhookEndpointEnablePost({
        ...common,
        path: { ...common.path, endpoint_id: command.endpointId },
        body: { enabled: command.enabled, reason: command.reason },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "webhook.rotate": {
      const result = await adminV1WebhookEndpointRotatePost({
        ...common,
        path: { ...common.path, endpoint_id: command.endpointId },
        body: {
          confirmation: confirmation("rotates_signing_key"),
          overlap_seconds: command.overlapSeconds,
          reason: command.reason,
        },
      });
      return unwrapApiResult<ResourceEnvelopeOneTimeSigningValueResponse>(
        result,
      ).data;
    }
    case "webhook.test": {
      const result = await adminV1WebhookEndpointTestPost({
        ...common,
        path: { ...common.path, endpoint_id: command.endpointId },
        body: { reason: command.reason },
      });
      return unwrapApiResult<ResourceEnvelopeWebhookTestResponse>(result).data;
    }
    case "webhook.delete": {
      const result = await adminV1WebhookEndpointDeletePost({
        ...common,
        path: { ...common.path, endpoint_id: command.endpointId },
        body: {
          confirmation: confirmation("deletes_integration"),
          reason: command.reason,
        },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "delivery.retry": {
      const result = await adminV1WebhookDeliveryRetryPost({
        ...common,
        path: { ...common.path, delivery_id: command.deliveryId },
        body: {
          confirmation: confirmation("retries_delivery"),
          reason: command.reason,
        },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
    case "notification-delivery.retry": {
      const result = await adminV1NotificationDeliveryRetryPost({
        ...common,
        path: { ...common.path, delivery_id: command.deliveryId },
        body: {
          confirmation: confirmation("retries_delivery"),
          reason: command.reason,
        },
      });
      return unwrapApiResult<ResourceEnvelopeIntegrationCommandResponse>(result)
        .data;
    }
  }
}
