import {
  adminV1BulkExecutePost,
  adminV1BulkPreviewPost,
  adminV1ExportPost,
} from "./generated/sdk.gen";
import type {
  AccountBulkFilter,
  BulkOperation,
  BulkPreviewResponse,
  BulkReportResponse,
  ExportCommand,
  ResourceEnvelopeBulkPreviewResponse,
  ResourceEnvelopeBulkReportResponse,
} from "./generated/types.gen";
import { createAdminClient, unwrapApiResult } from "./transport";

const client = createAdminClient();

export async function previewBulk(input: {
  filter: AccountBulkFilter;
  operation: BulkOperation;
  reason: string;
  spaceSlug: string;
}): Promise<BulkPreviewResponse> {
  const result = await adminV1BulkPreviewPost({
    client,
    path: { space_slug: input.spaceSlug },
    headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
    body: {
      reason: input.reason,
      filter: input.filter,
      operation: input.operation,
    },
  });
  return unwrapApiResult<ResourceEnvelopeBulkPreviewResponse>(result).data;
}

export async function executeBulk(input: {
  reason: string;
  snapshotId: string;
  spaceSlug: string;
}): Promise<BulkReportResponse> {
  const result = await adminV1BulkExecutePost({
    client,
    path: { space_slug: input.spaceSlug },
    headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
    body: {
      reason: input.reason,
      snapshot_id: input.snapshotId,
      confirmation: {
        confirmed: true,
        consequence_codes: ["changes_many_accounts"],
      },
    },
  });
  return unwrapApiResult<ResourceEnvelopeBulkReportResponse>(result).data;
}

export async function downloadExport(input: {
  command: ExportCommand;
  spaceSlug: string;
}): Promise<void> {
  const result = await adminV1ExportPost({
    client,
    path: { space_slug: input.spaceSlug },
    headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
    body: input.command,
  });
  const content = unwrapApiResult<unknown>(result);
  const body =
    typeof content === "string" ? content : JSON.stringify(content, null, 2);
  const blob = new Blob([body], {
    type:
      input.command.format === "csv"
        ? "text/csv;charset=utf-8"
        : "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${input.command.source}.${input.command.format}`;
  link.click();
  URL.revokeObjectURL(url);
}
