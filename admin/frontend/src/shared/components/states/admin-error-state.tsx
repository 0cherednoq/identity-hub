import { AlertTriangle, WifiOff } from "lucide-react";

import { AdminApiError } from "@/shared/api/transport";
import { useErrorReporting } from "@/shared/config/error-reporting";
import { Button } from "@/shared/ui/button";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/shared/ui/empty";

const HTTP_COPY: Record<number, { title: string; description: string }> = {
  401: { title: "Нужен вход", description: "Сессия отсутствует или истекла." },
  403: {
    title: "Недостаточно прав",
    description: "Доступ к ресурсу запрещён.",
  },
  404: { title: "Не найдено", description: "Ресурс удалён или адрес неверен." },
  409: {
    title: "Конфликт",
    description: "Данные уже изменились. Обновите страницу.",
  },
  412: {
    title: "Версия устарела",
    description: "Обновите данные перед повтором.",
  },
};

export function AdminErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const offline = typeof navigator !== "undefined" && !navigator.onLine;
  const known =
    error instanceof AdminApiError ? HTTP_COPY[error.status] : undefined;
  const title = offline
    ? "Нет сети"
    : (known?.title ?? "Непредвиденная ошибка");
  const description = offline
    ? "Проверьте соединение и повторите запрос."
    : (known?.description ??
      (error instanceof Error ? error.message : "Повторите позже."));
  const requestId =
    error instanceof AdminApiError ? error.body?.error.request_id : undefined;
  useErrorReporting(
    !offline && known === undefined
      ? {
          error,
          ...(requestId === undefined ? {} : { requestId }),
          source: "admin-error-state",
        }
      : null,
  );

  return (
    <Empty role="alert">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          {offline ? (
            <WifiOff aria-hidden="true" />
          ) : (
            <AlertTriangle aria-hidden="true" />
          )}
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
        {requestId === undefined ? null : (
          <EmptyDescription>Request ID: {requestId}</EmptyDescription>
        )}
      </EmptyHeader>
      {onRetry === undefined ? null : (
        <EmptyContent>
          <Button type="button" onClick={onRetry}>
            Повторить
          </Button>
        </EmptyContent>
      )}
    </Empty>
  );
}
