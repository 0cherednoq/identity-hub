import { useState } from "react";

import type {
  AccountBulkFilter,
  BulkOperation,
  BulkPreviewResponse,
  BulkReportResponse,
} from "@/shared/api/generated/types.gen";
import { downloadExport, executeBulk, previewBulk } from "@/shared/api/bulk";
import { AdminApiError } from "@/shared/api/transport";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";

type OperationKind = BulkOperation["kind"];

function operation(
  kind: OperationKind,
  target: string,
  values: string,
): BulkOperation {
  const list = values
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (kind === "lifecycle") {
    return {
      kind,
      target: target as "provisioning" | "ready" | "retired",
    };
  }
  if (kind === "resync") return { kind, regions: list };
  return { kind, tags: list };
}

function message(cause: unknown): string {
  return cause instanceof AdminApiError || cause instanceof Error
    ? cause.message
    : "Операция отклонена.";
}

export function BulkControls({
  filter,
  spaceSlug,
  onComplete,
}: {
  filter: AccountBulkFilter;
  spaceSlug: string;
  onComplete: () => void;
}) {
  const [kind, setKind] = useState<OperationKind>("tag_add");
  const [target, setTarget] = useState("ready");
  const [values, setValues] = useState("reviewed");
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<BulkPreviewResponse | null>(null);
  const [report, setReport] = useState<BulkReportResponse | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const runPreview = async () => {
    setPending(true);
    setError("");
    setReport(null);
    try {
      setPreview(
        await previewBulk({
          filter,
          operation: operation(kind, target, values),
          reason,
          spaceSlug,
        }),
      );
      setConfirmed(false);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setPending(false);
    }
  };

  const runExecute = async () => {
    if (preview === null) return;
    setPending(true);
    setError("");
    try {
      const value = await executeBulk({
        reason,
        snapshotId: preview.snapshot_id,
        spaceSlug,
      });
      setReport(value);
      onComplete();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setPending(false);
    }
  };

  const exportFile = async (format: "csv" | "json") => {
    setPending(true);
    setError("");
    try {
      await downloadExport({
        spaceSlug,
        command: {
          source: "accounts",
          format,
          reason,
          account_filter: filter,
        },
      });
    } catch (cause) {
      setError(message(cause));
    } finally {
      setPending(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Все аккаунты по текущему фильтру</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <p className="text-muted-foreground text-sm">
          Preview фиксирует ID и revision на сервере. Новые аккаунты после
          preview в batch не попадут.
        </p>
        <div className="grid gap-3 md:grid-cols-3">
          <label className="grid gap-1 text-sm">
            Действие
            <select
              className="border-input bg-background h-9 rounded-md border px-3"
              value={kind}
              onChange={(event) => {
                setKind(event.target.value as OperationKind);
                setPreview(null);
              }}
            >
              <option value="tag_add">Добавить теги</option>
              <option value="tag_remove">Удалить теги</option>
              <option value="lifecycle">Изменить lifecycle</option>
              <option value="resync">Выполнить resync</option>
            </select>
          </label>
          {kind === "lifecycle" ? (
            <label className="grid gap-1 text-sm">
              Новый lifecycle
              <select
                className="border-input bg-background h-9 rounded-md border px-3"
                value={target}
                onChange={(event) => {
                  setTarget(event.target.value);
                  setPreview(null);
                }}
              >
                <option value="provisioning">Provisioning</option>
                <option value="ready">Ready</option>
                <option value="retired">Retired</option>
              </select>
            </label>
          ) : (
            <label className="grid gap-1 text-sm">
              {kind === "resync"
                ? "Регионы через запятую"
                : "Теги через запятую"}
              <Input
                value={values}
                onChange={(event) => {
                  setValues(event.target.value);
                  setPreview(null);
                }}
              />
            </label>
          )}
          <label className="grid gap-1 text-sm">
            Причина
            <Input
              value={reason}
              minLength={3}
              maxLength={500}
              onChange={(event) => {
                setReason(event.target.value);
              }}
            />
          </label>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={pending || reason.trim().length < 3}
            onClick={() => {
              void runPreview();
            }}
          >
            Preview batch
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={pending || reason.trim().length < 3}
            onClick={() => {
              void exportFile("csv");
            }}
          >
            Экспорт CSV
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={pending || reason.trim().length < 3}
            onClick={() => {
              void exportFile("json");
            }}
          >
            Экспорт JSON
          </Button>
        </div>
        {preview === null ? null : (
          <section
            className="grid gap-3 rounded-md border p-4"
            aria-label="Preview batch"
          >
            <p>
              Зафиксировано: {preview.selected_count}
              {preview.truncated ? " (достигнут лимит 1000)" : ""}. Snapshot
              действует до{" "}
              {new Date(preview.expires_at).toLocaleString("ru-RU")}.
            </p>
            <ul className="text-muted-foreground text-sm">
              {preview.sample.map((item) => (
                <li key={item.identity_id}>
                  {item.service} · {item.external_ref} · revision{" "}
                  {item.revision}
                </li>
              ))}
            </ul>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => {
                  setConfirmed(event.target.checked);
                }}
              />
              Я подтверждаю изменение зафиксированного набора аккаунтов
            </label>
            <Button
              type="button"
              disabled={pending || !confirmed || preview.selected_count === 0}
              onClick={() => {
                void runExecute();
              }}
            >
              Выполнить batch
            </Button>
          </section>
        )}
        {report === null ? null : (
          <p role="status">
            Batch attempt {report.attempt}: успешно {report.succeeded} /{" "}
            {report.total}, ошибок {report.failed}, ранее выполнено{" "}
            {report.skipped_already_succeeded}.
          </p>
        )}
        {error ? (
          <p className="text-destructive text-sm" role="alert">
            {error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
