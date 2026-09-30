import { Link } from "@tanstack/react-router";

import type {
  HumanQueueItemResponse,
  HumanQueuePageResponse,
} from "@/shared/api/generated/types.gen";
import type { HumanQueueKind } from "@/shared/lib/human-queue-search";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/shared/ui/tooltip";

const sectionLabels: Record<HumanQueueKind, string> = {
  challenge: "Открытые challenges",
  requirement: "Требования платформы",
  manual_review: "Ручные проверки",
  drift: "Drift после override",
  quarantine: "Карантин",
};

const urgencyLabels = {
  critical: "Критическая",
  high: "Высокая",
  medium: "Средняя",
} as const;

function Deadline({
  item,
  now,
}: {
  item: HumanQueueItemResponse;
  now: number;
}) {
  if (item.deadline === null) return <span>—</span>;
  const deadline = new Date(item.deadline);
  const minutes = Math.round((deadline.getTime() - now) / 60_000);
  const relative = new Intl.RelativeTimeFormat("ru-RU", {
    numeric: "auto",
  }).format(minutes, "minute");
  return (
    <div className="grid gap-1">
      <span>{deadline.toLocaleString("ru-RU")}</span>
      <span className="text-muted-foreground text-xs">{relative}</span>
      {item.expired ? <Badge variant="destructive">Просрочено</Badge> : null}
    </div>
  );
}

function QueueRows({
  items,
  spaceSlug,
  now,
}: {
  items: HumanQueueItemResponse[];
  spaceSlug: string;
  now: number;
}) {
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">В этом разделе нет задач.</p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Аккаунт</TableHead>
            <TableHead>Причина</TableHead>
            <TableHead>Срочность</TableHead>
            <TableHead>Дедлайн</TableHead>
            <TableHead className="text-right">Действие</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item) => (
            <TableRow key={item.id}>
              <TableCell>
                <Link
                  className="font-medium underline-offset-4 hover:underline"
                  to="/spaces/$spaceSlug/accounts/$identityId"
                  params={{ spaceSlug, identityId: item.identity_id }}
                  search={{ tab: item.action_tab }}
                >
                  {item.service} · {item.external_ref}
                </Link>
                <div className="text-muted-foreground text-xs">{item.key}</div>
              </TableCell>
              <TableCell>{item.reason}</TableCell>
              <TableCell>
                <Badge
                  variant={
                    item.urgency === "critical" ? "destructive" : "outline"
                  }
                >
                  {urgencyLabels[item.urgency]}
                </Badge>
              </TableCell>
              <TableCell>
                <Deadline item={item} now={now} />
              </TableCell>
              <TableCell className="text-right">
                {item.action_available ? (
                  <Button asChild type="button" size="sm">
                    <Link
                      to="/spaces/$spaceSlug/accounts/$identityId"
                      params={{ spaceSlug, identityId: item.identity_id }}
                      search={{ tab: item.action_tab }}
                    >
                      Обработать
                    </Link>
                  </Button>
                ) : (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="inline-block" tabIndex={0}>
                          <Button type="button" size="sm" disabled>
                            Обработать
                          </Button>
                        </span>
                      </TooltipTrigger>
                      <TooltipContent>
                        {item.action_unavailable_reason}
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function HumanQueueContent({
  data,
  spaceSlug,
  selectedKind,
  now,
  hasPrevious,
  onPrevious,
  onNext,
}: {
  data: HumanQueuePageResponse;
  spaceSlug: string;
  selectedKind?: HumanQueueKind | undefined;
  now: number;
  hasPrevious: boolean;
  onPrevious: () => void;
  onNext: (cursor: string) => void;
}) {
  const kinds = (
    selectedKind === undefined
      ? (Object.keys(sectionLabels) as HumanQueueKind[])
      : [selectedKind]
  ).filter(
    (kind) => selectedKind !== undefined || (data.counts[kind] ?? 0) > 0,
  );
  return (
    <div className="grid gap-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {(Object.keys(sectionLabels) as HumanQueueKind[]).map((kind) => (
          <Card key={kind}>
            <CardContent className="pt-6">
              <p className="text-muted-foreground text-xs">
                {sectionLabels[kind]}
              </p>
              <p className="mt-1 text-2xl font-semibold">
                {data.counts[kind] ?? 0}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
      {kinds.map((kind) => (
        <Card key={kind}>
          <CardHeader>
            <CardTitle>
              {sectionLabels[kind]} · {data.counts[kind] ?? 0}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <QueueRows
              items={data.items.filter((item) => item.kind === kind)}
              spaceSlug={spaceSlug}
              now={now}
            />
          </CardContent>
        </Card>
      ))}
      <div className="flex justify-end gap-2">
        <Button
          type="button"
          variant="outline"
          disabled={!hasPrevious}
          onClick={onPrevious}
        >
          Назад
        </Button>
        <Button
          type="button"
          disabled={data.next_cursor === null}
          onClick={() => {
            if (data.next_cursor !== null) onNext(data.next_cursor);
          }}
        >
          Далее
        </Button>
      </div>
    </div>
  );
}
