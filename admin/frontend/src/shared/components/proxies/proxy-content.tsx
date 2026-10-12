import { Link } from "@tanstack/react-router";

import type {
  ProxyCardResponse,
  ProxyDetailResponse,
  ProxyPageResponse,
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/shared/ui/tooltip";

const percent = new Intl.NumberFormat("ru-RU", {
  style: "percent",
  maximumFractionDigits: 0,
});

function Disabled({ reason }: { reason?: string | null | undefined }) {
  return (
    <Badge variant="outline" title={reason ?? undefined}>
      выключен
    </Badge>
  );
}

function Status({ value }: { value: string }) {
  return (
    <Badge
      variant={
        value === "dead"
          ? "destructive"
          : value === "alive"
            ? "secondary"
            : "outline"
      }
    >
      {value}
    </Badge>
  );
}

function DisabledAction({ proxy }: { proxy: ProxyCardResponse }) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-block" tabIndex={0}>
            <Button type="button" size="sm" disabled>
              Проверить
            </Button>
          </span>
        </TooltipTrigger>
        <TooltipContent>{proxy.actions_unavailable_reason}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function ProxyOverviewContent({
  data,
  spaceSlug,
  hasPrevious,
  onPrevious,
  onNext,
}: {
  data: ProxyPageResponse;
  spaceSlug: string;
  hasPrevious: boolean;
  onPrevious: () => void;
  onNext: (cursor: string) => void;
}) {
  return (
    <div className="grid gap-4">
      <div className="grid gap-3 lg:grid-cols-3">
        {data.pools.map((pool) => (
          <Card key={pool.pool}>
            <CardHeader>
              <CardTitle>{pool.pool}</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-1 text-sm">
              <p>
                {pool.proxies} прокси · {pool.alive} alive
              </p>
              <p>
                {pool.bound_count} / {pool.max_identities} привязок
              </p>
              <p>
                {percent.format(pool.utilization)} загрузки ·{" "}
                {pool.in_lease_count} в аренде
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Прокси · {data.items.length}</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Адрес</TableHead>
                <TableHead>Пул</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead>Здоровье</TableHead>
                <TableHead>Ёмкость</TableHead>
                <TableHead>В аренде</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((proxy) => (
                <TableRow key={proxy.id}>
                  <TableCell>
                    <Link
                      className="font-mono underline-offset-4 hover:underline"
                      to="/spaces/$spaceSlug/proxies/$proxyId"
                      params={{ spaceSlug, proxyId: proxy.id }}
                      search={{}}
                    >
                      {proxy.masked_url}
                    </Link>
                  </TableCell>
                  <TableCell>{proxy.pool}</TableCell>
                  <TableCell>
                    <span className="flex flex-wrap items-center gap-1">
                      <Status value={proxy.status} />
                      {proxy.enabled === false ? (
                        <Disabled reason={proxy.disabled_reason} />
                      ) : null}
                    </span>
                  </TableCell>
                  <TableCell>
                    score {proxy.error_score.toFixed(1)} · streak{" "}
                    {proxy.fail_streak}
                    <br />
                    <span className="text-muted-foreground text-xs">
                      retry{" "}
                      {proxy.next_check_at === null
                        ? "—"
                        : new Date(proxy.next_check_at).toLocaleString("ru-RU")}
                    </span>
                  </TableCell>
                  <TableCell>
                    {proxy.bound_count} / {proxy.max_identities}
                  </TableCell>
                  <TableCell>{proxy.in_lease_count}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
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

export function ProxyDetailContent({
  data,
  spaceSlug,
  hasPrevious,
  onPrevious,
  onNext,
}: {
  data: ProxyDetailResponse;
  spaceSlug: string;
  hasPrevious: boolean;
  onPrevious: () => void;
  onNext: (cursor: string) => void;
}) {
  const proxy = data.proxy;
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <span className="font-mono">{proxy.masked_url}</span>
            <Status value={proxy.status} />
            {proxy.enabled === false ? (
              <Disabled reason={proxy.disabled_reason} />
            ) : null}
          </CardTitle>
          {proxy.enabled === false ? (
            <p className="text-muted-foreground text-sm">
              Выключен оператором{" "}
              {proxy.disabled_by ? `(${proxy.disabled_by})` : ""}:{" "}
              {proxy.disabled_reason ?? "причина не указана"}. Прокси не
              выдаётся; привязки и статус здоровья сохранены.
            </p>
          ) : null}
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-4">
          <p>
            <span className="text-muted-foreground text-xs">Пул</span>
            <br />
            {proxy.pool}
          </p>
          <p>
            <span className="text-muted-foreground text-xs">Привязки</span>
            <br />
            {proxy.bound_count} / {proxy.max_identities}
          </p>
          <p>
            <span className="text-muted-foreground text-xs">В аренде</span>
            <br />
            {proxy.in_lease_count}
          </p>
          {proxy.actions_available ? (
            <p className="text-muted-foreground text-sm">
              Действия доступны ниже.
            </p>
          ) : (
            <DisabledAction proxy={proxy} />
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>
            Привязанные аккаунты · {data.bound_identities.length}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {data.bound_identities.length === 0 ? (
            <p className="text-muted-foreground text-sm">Нет привязок.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Аккаунт</TableHead>
                  <TableHead>Привязан</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.bound_identities.map((item) => (
                  <TableRow key={item.identity_id}>
                    <TableCell>
                      <Link
                        className="underline-offset-4 hover:underline"
                        to="/spaces/$spaceSlug/accounts/$identityId"
                        params={{ spaceSlug, identityId: item.identity_id }}
                        search={{ tab: "network" }}
                      >
                        {item.service} · {item.external_ref}
                      </Link>
                    </TableCell>
                    <TableCell>
                      {new Date(item.bound_at).toLocaleString("ru-RU")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>История привязок</CardTitle>
        </CardHeader>
        <CardContent>
          {data.history.length === 0 ? (
            <p className="text-muted-foreground text-sm">История пуста.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Время</TableHead>
                  <TableHead>Аккаунт</TableHead>
                  <TableHead>Причина</TableHead>
                  <TableHead>Actor</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.history.map((item) => (
                  <TableRow key={item.event_id}>
                    <TableCell>
                      {new Date(item.occurred_at).toLocaleString("ru-RU")}
                    </TableCell>
                    <TableCell>
                      {item.external_ref ?? item.identity_id ?? "—"}
                    </TableCell>
                    <TableCell>{item.reason}</TableCell>
                    <TableCell>{item.actor}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
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
          disabled={data.next_history_cursor === null}
          onClick={() => {
            if (data.next_history_cursor !== null)
              onNext(data.next_history_cursor);
          }}
        >
          Далее
        </Button>
      </div>
    </div>
  );
}
