import { Link } from "@tanstack/react-router";

import type {
  LeaseChartBucketResponse,
  LeaseDashboardResponse,
  LeaseHistoryPageResponse,
  LeaseWindowResponse,
  LiveLeaseResponse,
} from "@/shared/api/generated/types.gen";
import { DualTimeSeriesChart } from "@/shared/components/charts/time-series-chart";
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

const count = new Intl.NumberFormat("ru-RU");
const percent = new Intl.NumberFormat("ru-RU", {
  style: "percent",
  maximumFractionDigits: 1,
});

function duration(value: number | null): string {
  if (value === null) return "—";
  if (value < 60) return `${value.toFixed(1)} с`;
  if (value < 3600) return `${(value / 60).toFixed(1)} мин`;
  return `${(value / 3600).toFixed(1)} ч`;
}

const metricRows: Array<{
  label: string;
  value: (item: LeaseWindowResponse) => string;
}> = [
  { label: "Взято", value: (item) => count.format(item.acquired) },
  { label: "Завершено", value: (item) => count.format(item.completed) },
  {
    label: "Уникальных аккаунтов",
    value: (item) => count.format(item.unique_identities),
  },
  {
    label: "Средняя длительность",
    value: (item) => duration(item.average_seconds),
  },
  { label: "p95 длительности", value: (item) => duration(item.p95_seconds) },
  {
    label: "Lost + expired",
    value: (item) =>
      count.format(
        (item.end_reasons.lost ?? 0) + (item.end_reasons.expired ?? 0),
      ),
  },
  {
    label: "Операций",
    value: (item) => count.format(item.succeeded + item.failed),
  },
  {
    label: "Неудачных операций",
    value: (item) =>
      item.failure_rate === null ? "—" : percent.format(item.failure_rate),
  },
  {
    label: "Загрузка пространства",
    value: (item) =>
      item.utilization === null ? "—" : percent.format(item.utilization),
  },
  {
    label: "Пустых выборок",
    value: (item) =>
      item.empty_picks === null ? "после A4.2" : count.format(item.empty_picks),
  },
];

export function LeaseMetrics({ windows }: { windows: LeaseWindowResponse[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Метрики по окнам</CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Метрика</TableHead>
              {windows.map((item) => (
                <TableHead className="text-right" key={item.window}>
                  {item.window}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {metricRows.map((row) => (
              <TableRow key={row.label}>
                <TableCell className="font-medium">{row.label}</TableCell>
                {windows.map((item) => (
                  <TableCell className="text-right font-mono" key={item.window}>
                    {row.value(item)}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function chartData(items: LeaseChartBucketResponse[]) {
  return items.map((item) => ({
    label: new Date(item.start).toLocaleTimeString("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
    }),
    primary: item.acquired,
    secondary: item.lost_or_expired,
  }));
}

export function LeaseTrend({ items }: { items: LeaseChartBucketResponse[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Динамика за 24 часа</CardTitle>
      </CardHeader>
      <CardContent>
        <DualTimeSeriesChart data={chartData(items)} />
      </CardContent>
    </Card>
  );
}

export function LeaseSlices({ data }: { data: LeaseDashboardResponse }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Разрез за 24 часа · {data.group_by}</CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {data.slices.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            Нет данных для выбранного разреза.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Значение</TableHead>
                <TableHead className="text-right">Взято</TableHead>
                <TableHead className="text-right">Завершено</TableHead>
                <TableHead className="text-right">Lost + expired</TableHead>
                <TableHead className="text-right">Failure rate</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.slices.map((slice) => (
                <TableRow key={slice.value}>
                  <TableCell className="font-medium">
                    {slice.value}{" "}
                    {slice.loss_above_average ? (
                      <Badge variant="destructive">выше среднего</Badge>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-right">
                    {slice.metrics.acquired}
                  </TableCell>
                  <TableCell className="text-right">
                    {slice.metrics.completed}
                  </TableCell>
                  <TableCell className="text-right">
                    {(slice.metrics.end_reasons.lost ?? 0) +
                      (slice.metrics.end_reasons.expired ?? 0)}
                  </TableCell>
                  <TableCell className="text-right">
                    {slice.metrics.failure_rate === null
                      ? "—"
                      : percent.format(slice.metrics.failure_rate)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function LiveRows({
  items,
  spaceSlug,
}: {
  items: LiveLeaseResponse[];
  spaceSlug: string;
}) {
  return items.length === 0 ? (
    <p className="text-muted-foreground text-sm">Нет записей.</p>
  ) : (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Аккаунт</TableHead>
            <TableHead>Пул / holder</TableHead>
            <TableHead>Heartbeat</TableHead>
            <TableHead>Истекает</TableHead>
            <TableHead className="text-right">Операции</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item) => (
            <TableRow key={item.lease_id}>
              <TableCell>
                <Link
                  className="font-medium underline-offset-4 hover:underline"
                  to="/spaces/$spaceSlug/accounts/$identityId"
                  params={{ spaceSlug, identityId: item.identity_id }}
                  search={{}}
                >
                  {item.service} · {item.external_ref}
                </Link>
              </TableCell>
              <TableCell>
                {item.pool}
                <br />
                <span className="text-muted-foreground text-xs">
                  {item.holder}
                </span>
              </TableCell>
              <TableCell>
                <Badge
                  variant={item.heartbeat_stale ? "destructive" : "outline"}
                >
                  {duration(item.heartbeat_lag_seconds)} назад
                </Badge>
              </TableCell>
              <TableCell>
                {item.expires_at === null
                  ? "—"
                  : new Date(item.expires_at).toLocaleString("ru-RU")}
              </TableCell>
              <TableCell className="text-right font-mono">
                {item.succeeded + item.failed}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function LiveLeases({
  data,
  spaceSlug,
}: {
  data: LeaseDashboardResponse;
  spaceSlug: string;
}) {
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Живые аренды · {data.live.length}</CardTitle>
        </CardHeader>
        <CardContent>
          <LiveRows items={data.live} spaceSlug={spaceSlug} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>
            Истёкшие, но не закрытые · {data.expired_unclosed.length}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <LiveRows items={data.expired_unclosed} spaceSlug={spaceSlug} />
        </CardContent>
      </Card>
    </div>
  );
}

export function LeaseHistory({
  data,
  spaceSlug,
}: {
  data: LeaseHistoryPageResponse;
  spaceSlug: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>История аренд</CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {data.items.length === 0 ? (
          <p className="text-muted-foreground text-sm">История пуста.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Аккаунт</TableHead>
                <TableHead>Пул / holder</TableHead>
                <TableHead>Начало</TableHead>
                <TableHead>Длительность</TableHead>
                <TableHead>Причина</TableHead>
                <TableHead className="text-right">Успех / ошибки</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((item) => (
                <TableRow key={item.lease_id}>
                  <TableCell>
                    <Link
                      className="font-medium underline-offset-4 hover:underline"
                      to="/spaces/$spaceSlug/accounts/$identityId"
                      params={{ spaceSlug, identityId: item.identity_id }}
                      search={{ tab: "leases" }}
                    >
                      {item.service} · {item.external_ref}
                    </Link>
                  </TableCell>
                  <TableCell>
                    {item.pool}
                    <br />
                    <span className="text-muted-foreground text-xs">
                      {item.holder}
                    </span>
                  </TableCell>
                  <TableCell>
                    {new Date(item.acquired_at).toLocaleString("ru-RU")}
                  </TableCell>
                  <TableCell>{duration(item.duration_seconds)}</TableCell>
                  <TableCell>
                    {item.end_reason ?? <Badge variant="outline">active</Badge>}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {item.succeeded} / {item.failed}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

export function LeaseDashboardContent({
  data,
  spaceSlug,
}: {
  data: LeaseDashboardResponse;
  spaceSlug: string;
}) {
  return (
    <div className="grid gap-6">
      <LeaseMetrics windows={data.windows} />
      <LeaseTrend items={data.chart} />
      <LeaseSlices data={data} />
      <LiveLeases data={data} spaceSlug={spaceSlug} />
    </div>
  );
}

export function LeaseHistoryContent({
  data,
  spaceSlug,
  hasPrevious,
  onPrevious,
  onNext,
}: {
  data: LeaseHistoryPageResponse;
  spaceSlug: string;
  hasPrevious: boolean;
  onPrevious: () => void;
  onNext: (cursor: string) => void;
}) {
  return (
    <div className="grid gap-4">
      <LeaseHistory data={data} spaceSlug={spaceSlug} />
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
