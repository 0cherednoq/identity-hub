import { AlertCircle, Inbox } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/shared/ui/button";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/shared/ui/empty";
import { Spinner } from "@/shared/ui/spinner";

export interface QueryLike<T> {
  data?: T | undefined;
  error?: unknown;
  isError: boolean;
  isFetching?: boolean | undefined;
  isPending: boolean;
  refetch?: (() => unknown) | undefined;
}

interface QueryStateProps<T> {
  query: QueryLike<T>;
  skeleton: ReactNode;
  empty: { title: string; description: string };
  isEmpty: (data: T) => boolean;
  children: (data: T) => ReactNode;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Не удалось получить данные";
}

export function QueryState<T>({
  query,
  skeleton,
  empty,
  isEmpty,
  children,
}: QueryStateProps<T>) {
  if (query.isPending) {
    return <div aria-label="Загрузка">{skeleton}</div>;
  }

  if (query.isError) {
    return (
      <Empty role="alert">
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <AlertCircle aria-hidden="true" />
          </EmptyMedia>
          <EmptyTitle>Ошибка загрузки</EmptyTitle>
          <EmptyDescription>{errorMessage(query.error)}</EmptyDescription>
        </EmptyHeader>
        {query.refetch === undefined ? null : (
          <EmptyContent>
            <Button type="button" onClick={() => void query.refetch?.()}>
              Повторить
            </Button>
          </EmptyContent>
        )}
      </Empty>
    );
  }

  if (query.data === undefined || isEmpty(query.data)) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Inbox aria-hidden="true" />
          </EmptyMedia>
          <EmptyTitle>{empty.title}</EmptyTitle>
          <EmptyDescription>{empty.description}</EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <div className="relative">
      {query.isFetching === true ? (
        <Spinner className="absolute top-0 right-0" aria-label="Обновление" />
      ) : null}
      {children(query.data)}
    </div>
  );
}
