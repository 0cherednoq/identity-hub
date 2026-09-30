import { Link } from "@tanstack/react-router";

import type {
  EventItemResponse,
  EventPageResponse,
} from "@/shared/api/generated/types.gen";
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

function SeverityBadge({ value }: { value: string }) {
  return (
    <Badge
      variant={
        value === "critical"
          ? "destructive"
          : value === "warning"
            ? "outline"
            : "secondary"
      }
    >
      {value}
    </Badge>
  );
}

function EntityLinks({
  item,
  spaceSlug,
}: {
  item: EventItemResponse;
  spaceSlug: string;
}) {
  return (
    <div className="grid gap-1 text-xs">
      {item.identity_id === null ? null : (
        <Link
          className="underline-offset-4 hover:underline"
          to="/spaces/$spaceSlug/accounts/$identityId"
          params={{ spaceSlug, identityId: item.identity_id }}
          search={{ tab: "events" }}
        >
          аккаунт
        </Link>
      )}
      {item.proxy_id === null ? null : (
        <Link
          className="underline-offset-4 hover:underline"
          to="/spaces/$spaceSlug/proxies/$proxyId"
          params={{ spaceSlug, proxyId: item.proxy_id }}
          search={{}}
        >
          прокси
        </Link>
      )}
      {item.lease_id === null ? null : (
        <Link
          className="underline-offset-4 hover:underline"
          to="/spaces/$spaceSlug/leases"
          params={{ spaceSlug }}
          search={{}}
        >
          аренда
        </Link>
      )}
    </div>
  );
}

export function EventJournalContent({
  data,
  spaceSlug,
  selected,
  hasPrevious,
  onSelect,
  onPrevious,
  onNext,
}: {
  data: EventPageResponse;
  spaceSlug: string;
  selected: string | undefined;
  hasPrevious: boolean;
  onSelect: (eventId: string) => void;
  onPrevious: () => void;
  onNext: (cursor: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>События · {data.items.length}</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Время</TableHead>
              <TableHead>Тип</TableHead>
              <TableHead>Важность</TableHead>
              <TableHead>Объекты</TableHead>
              <TableHead>Actor</TableHead>
              <TableHead>Содержание</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.items.map((item) => (
              <TableRow
                key={item.event_id}
                data-state={selected === item.event_id ? "selected" : undefined}
              >
                <TableCell className="whitespace-nowrap">
                  {new Date(item.occurred_at).toLocaleString("ru-RU")}
                </TableCell>
                <TableCell>
                  <p className="font-mono text-xs">{item.type}</p>
                  <p className="text-muted-foreground text-xs">{item.group}</p>
                </TableCell>
                <TableCell>
                  <SeverityBadge value={item.severity} />
                </TableCell>
                <TableCell>
                  <EntityLinks item={item} spaceSlug={spaceSlug} />
                </TableCell>
                <TableCell className="max-w-44 truncate">
                  {item.actor}
                </TableCell>
                <TableCell className="max-w-72 truncate">
                  {item.summary}
                </TableCell>
                <TableCell>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      onSelect(item.event_id);
                    }}
                  >
                    Подробнее
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
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
      </CardContent>
    </Card>
  );
}
