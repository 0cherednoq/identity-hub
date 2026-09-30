import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { FileUp, RotateCcw, ShieldCheck, XCircle } from "lucide-react";
import { useState, type DragEvent } from "react";

import { useAuth } from "@/shared/auth/auth-context";
import type { ImportRunResponse } from "@/shared/api/generated/types.gen";
import { cancelImport, fetchImports, startImport } from "@/shared/api/imports";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { fetchServices } from "@/shared/api/services";
import { Section } from "@/shared/components/layout/layout";
import {
  parseBatchLabels,
  prepareImportFile,
  type PreparedImportFile,
} from "@/shared/lib/import-file";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

const ACTIVE_STATES = new Set(["pending", "running", "cancelling"]);

function stateLabel(state: ImportRunResponse["state"]): string {
  return {
    pending: "В очереди",
    running: "Выполняется",
    succeeded: "Завершён",
    failed: "Ошибка",
    cancelling: "Останавливается",
    cancelled: "Отменён",
  }[state];
}

function formatBytes(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${String(Math.ceil(bytes / 1024))} КиБ`
    : `${(bytes / 1024 / 1024).toFixed(1)} МиБ`;
}

function progress(run: ImportRunResponse): number {
  return run.total === 0 ? 0 : Math.round((run.processed / run.total) * 100);
}

export function ImportsContent({ spaceSlug }: { spaceSlug: string }) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<PreparedImportFile | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [service, setService] = useState("");
  const [reason, setReason] = useState("");
  const [labels, setLabels] = useState("");
  const [onExisting, setOnExisting] = useState<
    "skip" | "update_session" | "fail"
  >("skip");
  const [verifySessions, setVerifySessions] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const canMutate =
    auth.principal?.role === "operator" || auth.principal?.role === "admin";

  const runs = useQuery({
    queryKey: ["space", spaceSlug, "imports"],
    queryFn: ({ signal }) => fetchImports(spaceSlug, signal),
    refetchInterval: (query) =>
      query.state.data?.items.some((run) => ACTIVE_STATES.has(run.state)) ===
      true
        ? 2_000
        : POLL_INTERVAL.operational,
  });
  const services = useQuery({
    queryKey: ["services"],
    queryFn: ({ signal }) => fetchServices(signal),
    staleTime: POLL_INTERVAL.reference,
  });
  const start = useMutation({
    mutationFn: async () => {
      if (selected === null) throw new Error("Сначала выберите файл");
      return startImport({
        spaceSlug,
        command: {
          content: selected.content,
          format: selected.format,
          labels: parseBatchLabels(labels),
          on_existing: onExisting,
          reason,
          service,
          source: selected.name,
          verify_sessions: verifySessions,
        },
      });
    },
    onSuccess: async (run) => {
      setExpanded(run.id);
      setSelected(null);
      setFileError(null);
      await queryClient.invalidateQueries({
        queryKey: ["space", spaceSlug, "imports"],
      });
    },
  });
  const cancel = useMutation({
    mutationFn: (runId: string) =>
      cancelImport({
        reason: "operator cancelled import",
        runId,
        spaceSlug,
      }),
    onSuccess: async () =>
      queryClient.invalidateQueries({
        queryKey: ["space", spaceSlug, "imports"],
      }),
  });

  async function choose(file: File | undefined) {
    if (file === undefined) return;
    setFileError(null);
    try {
      setSelected(await prepareImportFile(file));
    } catch (error) {
      setSelected(null);
      setFileError(
        error instanceof Error ? error.message : "Не удалось прочитать файл",
      );
    }
  }

  function dropped(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    void choose(event.dataTransfer.files[0]);
  }

  const serviceItems = services.data?.items ?? [];
  const mutationError =
    start.error instanceof Error ? start.error.message : null;
  const items = runs.data?.items ?? [];

  return (
    <div className="grid gap-6">
      <div className="grid gap-6 lg:grid-cols-3">
        <Section
          title="Новый импорт"
          description="До 10 МиБ и 10 000 записей. Поддерживаются .csv, .jsonl и .ndjson."
        >
          <form
            className="grid gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              start.mutate();
            }}
          >
            <div
              className="border-muted-foreground/40 hover:bg-muted/40 grid min-h-40 place-items-center rounded-lg border-2 border-dashed p-6 text-center transition-colors"
              onDragOver={(event) => {
                event.preventDefault();
              }}
              onDrop={dropped}
            >
              <div className="grid justify-items-center gap-2">
                <FileUp
                  className="text-muted-foreground size-8"
                  aria-hidden="true"
                />
                <label className="font-medium" htmlFor="import-file">
                  Перетащите файл или выберите его
                </label>
                <Input
                  id="import-file"
                  className="max-w-sm"
                  type="file"
                  accept=".csv,.jsonl,.ndjson,text/csv,application/x-ndjson"
                  disabled={!canMutate || start.isPending}
                  onChange={(event) => void choose(event.target.files?.[0])}
                />
                {selected === null ? null : (
                  <p className="text-muted-foreground text-sm">
                    {selected.name} · {formatBytes(selected.bytes)} · примерно{" "}
                    {selected.records} записей
                  </p>
                )}
                {fileError === null ? null : (
                  <p className="text-destructive text-sm" role="alert">
                    {fileError}
                  </p>
                )}
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <label className="grid gap-2 text-sm font-medium">
                Сервис
                <Select
                  value={service}
                  onValueChange={setService}
                  disabled={!canMutate}
                >
                  <SelectTrigger className="w-full" aria-label="Сервис импорта">
                    <SelectValue placeholder="Выберите сервис" />
                  </SelectTrigger>
                  <SelectContent>
                    {serviceItems.map((item) => (
                      <SelectItem key={item.name} value={item.name}>
                        {item.title}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
              <label className="grid gap-2 text-sm font-medium">
                Если аккаунт существует
                <Select
                  value={onExisting}
                  onValueChange={(value) => {
                    setOnExisting(value as typeof onExisting);
                  }}
                  disabled={!canMutate}
                >
                  <SelectTrigger
                    className="w-full"
                    aria-label="Поведение для существующих аккаунтов"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="skip">Пропустить</SelectItem>
                    <SelectItem value="update_session">
                      Обновить сессию
                    </SelectItem>
                    <SelectItem value="fail">Считать ошибкой</SelectItem>
                  </SelectContent>
                </Select>
              </label>
              <label className="grid gap-2 text-sm font-medium sm:col-span-2">
                Причина
                <Input
                  value={reason}
                  maxLength={500}
                  disabled={!canMutate}
                  onChange={(event) => {
                    setReason(event.target.value);
                  }}
                  placeholder="Например: перенос утверждённой партии"
                />
              </label>
              <label className="grid gap-2 text-sm font-medium sm:col-span-2">
                Метки партии
                <Input
                  value={labels}
                  disabled={!canMutate}
                  onChange={(event) => {
                    setLabels(event.target.value);
                  }}
                  placeholder="team=ops, source=migration"
                />
              </label>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={verifySessions}
                disabled={!canMutate}
                onChange={(event) => {
                  setVerifySessions(event.target.checked);
                }}
              />
              Проверять импортированные сессии
            </label>
            {!canMutate ? (
              <p className="text-muted-foreground text-sm">
                Роль viewer может просматривать запуски, но не начинать и не
                отменять импорт.
              </p>
            ) : null}
            {mutationError === null ? null : (
              <p className="text-destructive text-sm" role="alert">
                {mutationError}
              </p>
            )}
            <Button
              type="submit"
              className="w-fit"
              disabled={
                !canMutate ||
                selected === null ||
                service === "" ||
                reason.trim() === "" ||
                start.isPending
              }
            >
              {start.isPending ? (
                <RotateCcw className="animate-spin" />
              ) : (
                <ShieldCheck />
              )}
              Запустить импорт
            </Button>
          </form>
        </Section>

        <Section
          title="Гарантии"
          description="Что происходит после отправки файла."
        >
          <ul className="text-muted-foreground grid list-disc gap-3 pl-4 text-sm">
            <li>Сервер строго проверяет формат, поля и ограничения.</li>
            <li>Исходный файл не сохраняется; staging зашифрован.</li>
            <li>После перезапуска обработка продолжится без дублей.</li>
            <li>Отмена вступает в силу на границе текущего пакета.</li>
            <li>Отчёт не содержит credentials и session payload.</li>
          </ul>
        </Section>
      </div>

      <Section
        title="Запуски"
        description="Состояние обновляется автоматически, пока есть активные импорты."
      >
        {runs.isPending ? (
          <p className="text-muted-foreground text-sm">Загрузка…</p>
        ) : runs.isError ? (
          <p className="text-destructive text-sm" role="alert">
            Не удалось загрузить историю импортов.
          </p>
        ) : items.length === 0 ? (
          <p className="text-muted-foreground text-sm">Импортов ещё не было.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Файл</TableHead>
                <TableHead>Сервис</TableHead>
                <TableHead>Состояние</TableHead>
                <TableHead>Прогресс</TableHead>
                <TableHead>Итог</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((run) => (
                <RunRows
                  key={run.id}
                  run={run}
                  spaceSlug={spaceSlug}
                  expanded={expanded === run.id}
                  canMutate={canMutate}
                  cancelling={cancel.isPending}
                  onToggle={() => {
                    setExpanded(expanded === run.id ? null : run.id);
                  }}
                  onCancel={() => {
                    cancel.mutate(run.id);
                  }}
                />
              ))}
            </TableBody>
          </Table>
        )}
      </Section>
    </div>
  );
}

function RunRows({
  run,
  spaceSlug,
  expanded,
  canMutate,
  cancelling,
  onToggle,
  onCancel,
}: {
  run: ImportRunResponse;
  spaceSlug: string;
  expanded: boolean;
  canMutate: boolean;
  cancelling: boolean;
  onToggle: () => void;
  onCancel: () => void;
}) {
  return (
    <>
      <TableRow>
        <TableCell>
          <button
            type="button"
            className="font-medium hover:underline"
            onClick={onToggle}
          >
            {run.source}
          </button>
          <div className="text-muted-foreground text-xs">
            {new Date(run.started_at).toLocaleString()}
          </div>
        </TableCell>
        <TableCell>{run.service}</TableCell>
        <TableCell>
          <Badge
            variant={
              run.failed > 0 || run.state === "failed"
                ? "destructive"
                : "outline"
            }
          >
            {stateLabel(run.state)}
          </Badge>
        </TableCell>
        <TableCell>
          <div className="grid min-w-36 gap-1">
            <progress
              className="h-2 w-full"
              max={run.total}
              value={run.processed}
              aria-label={`Прогресс ${run.source}`}
            />
            <span className="text-muted-foreground text-xs">
              {run.processed} / {run.total} · {progress(run)}%
            </span>
          </div>
        </TableCell>
        <TableCell>
          {run.created} создано · {run.updated} обновлено · {run.skipped}{" "}
          пропущено · {run.failed} ошибок
        </TableCell>
        <TableCell>
          {canMutate && ACTIVE_STATES.has(run.state) ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={cancelling || run.state === "cancelling"}
              onClick={onCancel}
            >
              <XCircle /> Отменить
            </Button>
          ) : null}
        </TableCell>
      </TableRow>
      {expanded ? (
        <TableRow>
          <TableCell colSpan={6} className="bg-muted/30 whitespace-normal">
            <div className="grid gap-4 p-2 text-sm md:grid-cols-3">
              <div>
                <p className="font-medium">Созданные аккаунты</p>
                {run.created_ids.length === 0 ? (
                  <p className="text-muted-foreground">Нет</p>
                ) : (
                  <ul className="mt-1 grid gap-1">
                    {run.created_ids.map((id) => (
                      <li key={id}>
                        <Link
                          className="text-primary hover:underline"
                          to="/spaces/$spaceSlug/accounts/$identityId"
                          params={{ spaceSlug, identityId: id }}
                          search={{}}
                        >
                          Открыть {id.slice(0, 8)}…
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <ReportItems
                title="Пропущенные записи"
                items={run.skipped_items}
              />
              <ReportItems title="Ошибки" items={run.failed_items} />
            </div>
          </TableCell>
        </TableRow>
      ) : null}
    </>
  );
}

function ReportItems({
  title,
  items,
}: {
  title: string;
  items: ImportRunResponse["failed_items"];
}) {
  return (
    <div>
      <p className="font-medium">{title}</p>
      {items.length === 0 ? (
        <p className="text-muted-foreground">Нет</p>
      ) : (
        <ul className="mt-1 grid gap-1">
          {items.map((item) => (
            <li
              key={`${String(item.position)}-${item.external_ref ?? "unknown"}`}
            >
              Строка {item.position}: {item.external_ref ?? "без ссылки"}
              {item.reason === null || item.reason === undefined
                ? ""
                : ` — ${item.reason}`}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
