import type {
  AnalyticsCatalogResponse,
  AnalyticsSeriesResponse,
  BanRateResponse,
  CohortResponse,
  FunnelResponse,
  ProxyQualityResponse,
  TimeToResponse,
  UtilizationResponse,
} from "@/shared/api/generated/types.gen";
import type { AnalyticsDashboardData } from "@/shared/api/analytics";
import { AnalyticsSeriesCard } from "@/shared/components/analytics/analytics-series-card";
import { MetricCard, MetricGrid } from "@/shared/components/metrics/metrics";
import { Section } from "@/shared/components/layout/layout";
import { Badge } from "@/shared/ui/badge";

const SERIES: Array<{
  metric: string;
  group: string;
  title: string;
  definition: string;
}> = [
  {
    metric: "identities.added",
    group: "Поступление аккаунтов",
    title: "Добавлено",
    definition:
      "Регистрация аккаунта или число созданных строк завершённого импорта.",
  },
  {
    metric: "auth.authenticated",
    group: "Авторизация",
    title: "Авторизовано",
    definition:
      "Переход auth в authenticated, а не текущее состояние аккаунта.",
  },
  {
    metric: "verifications.verified",
    group: "Верификация",
    title: "Верифицировано",
    definition: "Переход фактора в verified; разрез factor доступен из URL.",
  },
  {
    metric: "tiers.reached",
    group: "Верификация",
    title: "Достигнуты уровни",
    definition:
      "Атомарные события первого достижения каждого verification tier.",
  },
  {
    metric: "standings.entered",
    group: "Положение у платформы",
    title: "Тяжёлые standing-переходы",
    definition: "Переходы в limited, frozen и banned.",
  },
  {
    metric: "leases.acquired",
    group: "Нагрузка и лимиты",
    title: "Взято аренд",
    definition: "Аренды, начавшиеся внутри календарной корзины.",
  },
  {
    metric: "state.drift",
    group: "Качество данных и сверки",
    title: "Расхождения после сверки",
    definition:
      "Наблюдение платформы расходится с закреплённым локальным состоянием.",
  },
  {
    metric: "proxies.status",
    group: "Прокси",
    title: "Переходы здоровья прокси",
    definition: "Переходы в alive, quarantined и dead по пулу.",
  },
];

function seconds(value: number | null): string {
  if (value === null) return "—";
  if (value < 60) return `${value.toFixed(0)} с`;
  if (value < 3600) return `${(value / 60).toFixed(1)} мин`;
  return `${(value / 3600).toFixed(1)} ч`;
}

function Funnel({
  value,
  formula,
}: {
  value: FunnelResponse;
  formula: string;
}) {
  const max = Math.max(1, value.stages[0]?.count ?? 0);
  return (
    <Section title="Воронка" description={formula}>
      <ol className="grid gap-3">
        {value.stages.map((stage) => (
          <li key={stage.key} className="grid gap-1">
            <div className="flex justify-between gap-4 text-sm">
              <span className="break-all">{stage.key}</span>
              <strong className="font-mono tabular-nums">
                {stage.count.toLocaleString("ru-RU")} ·{" "}
                {(stage.share * 100).toFixed(1)}%
              </strong>
            </div>
            <div
              className="bg-muted h-2 overflow-hidden rounded-full"
              aria-hidden="true"
            >
              <div
                className="bg-primary h-full rounded-full"
                style={{ width: `${String((stage.count / max) * 100)}%` }}
              />
            </div>
          </li>
        ))}
      </ol>
    </Section>
  );
}

function TimeTo({
  value,
  formula,
}: {
  value: TimeToResponse;
  formula: string;
}) {
  return (
    <Section title="Время до авторизации" description={formula}>
      <MetricGrid>
        <MetricCard label="Наблюдений" value={value.samples} />
        <MetricCard
          label="Медиана"
          value={value.median_seconds ?? 0}
          description={seconds(value.median_seconds)}
        />
        <MetricCard
          label="p90"
          value={value.p90_seconds ?? 0}
          description={seconds(value.p90_seconds)}
        />
      </MetricGrid>
    </Section>
  );
}

function Cohorts({
  value,
  formula,
}: {
  value: CohortResponse;
  formula: string;
}) {
  return (
    <Section
      title="Когорты standing=good"
      description={formula}
      actions={
        <Badge
          variant={value.coverage === "complete" ? "secondary" : "outline"}
        >
          {value.coverage === "complete"
            ? "Полное покрытие"
            : "История неполна"}
        </Badge>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-max text-sm">
          <thead>
            <tr className="border-b text-left">
              <th className="p-2">Неделя</th>
              <th className="p-2">Размер</th>
              {[1, 7, 14, 30].map((day) => (
                <th key={day} className="p-2">
                  День {day}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {value.rows.map((row) => (
              <tr key={row.cohort_start} className="border-b last:border-0">
                <td className="p-2">
                  {new Date(row.cohort_start).toLocaleDateString("ru-RU")}
                </td>
                <td className="p-2 font-mono">{row.size}</td>
                {row.cells.map((cell) => (
                  <td
                    key={cell.offset_days}
                    className="p-2 font-mono tabular-nums"
                  >
                    {cell.share === null
                      ? "—"
                      : `${(cell.share * 100).toFixed(1)}%`}
                    <span className="text-muted-foreground ml-1 text-xs">
                      ({cell.good}/{cell.eligible})
                    </span>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {value.rows.length === 0 ? (
          <p className="text-muted-foreground py-8 text-center text-sm">
            В выбранном окне нет когорт.
          </p>
        ) : null}
      </div>
    </Section>
  );
}

function BanRate({
  value,
  formula,
}: {
  value: BanRateResponse;
  formula: string;
}) {
  return (
    <Section title={`Бан-рейт · ${value.group_by}`} description={formula}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left">
              <th className="p-2">Группа</th>
              <th className="p-2">Добавлено</th>
              <th className="p-2">Забанено</th>
              <th className="p-2">Доля</th>
            </tr>
          </thead>
          <tbody>
            {value.items.map((item) => (
              <tr key={item.key} className="border-b last:border-0">
                <td className="p-2 break-all">{item.key}</td>
                <td className="p-2 font-mono">{item.added}</td>
                <td className="p-2 font-mono">{item.banned}</td>
                <td className="p-2 font-mono">
                  {(item.rate * 100).toFixed(1)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {value.items.length === 0 ? (
          <p className="text-muted-foreground py-8 text-center text-sm">
            Нет групп с добавленными аккаунтами.
          </p>
        ) : null}
      </div>
    </Section>
  );
}

function Utilization({
  value,
  formula,
}: {
  value: UtilizationResponse;
  formula: string;
}) {
  const cellKey = (day: number, hour: number) =>
    `${String(day)}:${String(hour)}`;
  const byCell = new Map(
    value.heatmap.map((cell) => [cellKey(cell.weekday, cell.hour), cell]),
  );
  const maximum = Math.max(1, ...value.heatmap.map((cell) => cell.leases));
  return (
    <Section title="Загрузка и тепловая карта" description={formula}>
      <MetricGrid>
        <MetricCard
          label="Загрузка"
          value={(value.utilization ?? 0) * 100}
          description={
            value.utilization === null
              ? "Нет знаменателя"
              : `${(value.utilization * 100).toFixed(1)}%`
          }
        />
        <MetricCard label="Секунд в аренде" value={value.leased_seconds} />
        <MetricCard label="Пригодно сейчас" value={value.eligible_current} />
      </MetricGrid>
      <div className="mt-6 overflow-x-auto">
        <table className="w-full min-w-full text-center text-xs">
          <caption className="sr-only">
            Аренды по дню недели и часу пространства
          </caption>
          <thead>
            <tr>
              <th className="p-1 text-left">День</th>
              {Array.from({ length: 24 }, (_, hour) => (
                <th key={hour} className="p-1 font-normal">
                  {hour}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: 7 }, (_, day) => (
              <tr key={day}>
                <th className="p-1 text-left">
                  {["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][day]}
                </th>
                {Array.from({ length: 24 }, (_, hour) => {
                  const cell = byCell.get(cellKey(day, hour));
                  const leases = cell?.leases ?? 0;
                  return (
                    <td key={hour} className="p-1">
                      <span
                        className="inline-flex size-7 items-center justify-center rounded-md"
                        style={{
                          backgroundColor: `color-mix(in srgb, var(--color-primary) ${String(8 + (leases / maximum) * 82)}%, transparent)`,
                        }}
                        title={`${String(leases)} аренд; ${String(cell?.operations ?? 0)} операций; ${String(cell?.failed ?? 0)} ошибок`}
                      >
                        {leases}
                      </span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function ProxyQuality({
  value,
  formula,
}: {
  value: ProxyQualityResponse;
  formula: string;
}) {
  return (
    <Section title="Качество прокси" description={formula}>
      <MetricGrid>
        <MetricCard label="Ошибки с виной прокси" value={value.failures} />
        <MetricCard label="В карантин" value={value.quarantined} />
        <MetricCard label="Dead" value={value.dead} />
        <MetricCard label="Восстановлено" value={value.recovered} />
      </MetricGrid>
    </Section>
  );
}

function SeriesCard({
  value,
  catalog,
  title,
  definition,
}: {
  value: AnalyticsSeriesResponse | undefined;
  catalog: AnalyticsCatalogResponse;
  title: string;
  definition: string;
}) {
  return value === undefined ? null : (
    <AnalyticsSeriesCard
      series={value}
      title={title}
      definition={`${definition} ${catalog.formulas.series ?? ""}`}
      timezone={catalog.timezone}
    />
  );
}

export function AnalyticsDashboard({
  data,
  catalog,
}: {
  data: AnalyticsDashboardData;
  catalog: AnalyticsCatalogResponse;
}) {
  return (
    <div className="grid gap-6">
      <div className="bg-muted/40 rounded-lg border p-4 text-sm">
        <strong>Таймзона: {catalog.timezone}</strong>. Пустая корзина равна
        нулю; отсутствие snapshot coverage отмечается отдельно. Максимум ответа:{" "}
        {catalog.max_points} точек и {catalog.max_groups} групп.
      </div>
      {SERIES.map((item, index) => {
        const showHeading =
          index === 0 || SERIES[index - 1]?.group !== item.group;
        return (
          <div key={item.metric} className="grid gap-4">
            {showHeading ? (
              <h2 className="text-lg font-semibold">{item.group}</h2>
            ) : null}
            <SeriesCard
              value={data.series[item.metric]}
              catalog={catalog}
              title={item.title}
              definition={item.definition}
            />
            {item.metric === "identities.added" ? (
              <Funnel
                value={data.funnel}
                formula={catalog.formulas.funnel ?? ""}
              />
            ) : null}
            {item.metric === "auth.authenticated" ? (
              <TimeTo
                value={data.authTime}
                formula={catalog.formulas.time_to ?? ""}
              />
            ) : null}
            {item.metric === "standings.entered" ? (
              <>
                <Cohorts
                  value={data.cohorts}
                  formula={catalog.formulas.cohorts ?? ""}
                />
                <BanRate
                  value={data.banRate}
                  formula={catalog.formulas.ban_rate ?? ""}
                />
              </>
            ) : null}
            {item.metric === "leases.acquired" ? (
              <Utilization
                value={data.utilization}
                formula={catalog.formulas.utilization ?? ""}
              />
            ) : null}
            {item.metric === "proxies.status" ? (
              <ProxyQuality
                value={data.proxyQuality}
                formula={catalog.formulas.proxy_quality ?? ""}
              />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
