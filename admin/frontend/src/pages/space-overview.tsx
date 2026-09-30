import { useQuery } from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearch,
} from "@tanstack/react-router";

import type { SpaceOverviewResponse } from "@/shared/api/generated/types.gen";
import { fetchSpaceOverview } from "@/shared/api/overview";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { Section } from "@/shared/components/layout/layout";
import { MetricCard, MetricGrid } from "@/shared/components/metrics/metrics";
import { formatPercent } from "@/shared/components/overview/format";
import { ServiceOverview } from "@/shared/components/overview/overview";
import { QueryState } from "@/shared/components/states/query-state";
import { DashboardPage } from "@/shared/components/templates/templates";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/shared/ui/empty";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";
import { Skeleton } from "@/shared/ui/skeleton";

export function SpaceOverviewPage() {
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug",
  });
  const search = useSearch({ from: "/_authenticated/spaces/$spaceSlug" });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug" });
  const allQuery = useQuery({
    queryKey: ["space", spaceSlug, "overview", "all"],
    queryFn: ({ signal }) => fetchSpaceOverview(spaceSlug, undefined, signal),
    refetchInterval: POLL_INTERVAL.operational,
  });
  const query = useQuery({
    queryKey: ["space", spaceSlug, "overview", search.service ?? "all"],
    queryFn: ({ signal }) =>
      fetchSpaceOverview(spaceSlug, search.service, signal),
    refetchInterval: POLL_INTERVAL.operational,
  });
  const services =
    allQuery.data?.services.map((service) => service.service) ?? [];

  return (
    <DashboardPage
      eyebrow="Пространство"
      title={query.data?.title ?? spaceSlug}
      description="Текущая ёмкость и распределения по сервисам. Все значения получены из безопасной read-модели библиотеки."
      actions={
        <>
          <Button asChild variant="outline">
            <Link to="/spaces/$spaceSlug/imports" params={{ spaceSlug }}>
              Импорт
            </Link>
          </Button>
          <Button asChild variant="outline">
            <Link
              to="/spaces/$spaceSlug/accounts"
              params={{ spaceSlug }}
              search={{
                ...(search.service === undefined
                  ? {}
                  : { service: search.service }),
                ...(search.status === undefined
                  ? {}
                  : { status: search.status }),
              }}
            >
              Аккаунты
            </Link>
          </Button>
          <Select
            value={search.service ?? "all"}
            onValueChange={(value) =>
              void navigate({
                search: (previous) => ({
                  ...(previous.q === undefined ? {} : { q: previous.q }),
                  ...(previous.status === undefined
                    ? {}
                    : { status: previous.status }),
                  ...(value === "all" ? {} : { service: value }),
                }),
              })
            }
          >
            <SelectTrigger aria-label="Сервис" className="w-48">
              <SelectValue placeholder="Все сервисы" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все сервисы</SelectItem>
              {services.map((service) => (
                <SelectItem key={service} value={service}>
                  {service}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </>
      }
    >
      <QueryState<SpaceOverviewResponse>
        query={query}
        skeleton={
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }, (_, index) => (
              <Skeleton className="h-28" key={index} />
            ))}
          </div>
        }
        empty={{
          title: "Аккаунтов нет",
          description:
            "Пространство существует, но выбранный service slice пуст.",
        }}
        isEmpty={(data) => data.total === 0 && data.services.length === 0}
      >
        {(data) => (
          <div className="grid gap-6">
            <MetricGrid>
              <MetricCard label="Всего" value={data.total} />
              <MetricCard
                label="Пригодны"
                value={data.eligible}
                description={formatPercent(data.eligible, data.total)}
              />
              <MetricCard label="Свободны" value={data.free_eligible} />
              <MetricCard label="В аренде" value={data.leased} />
            </MetricGrid>

            {search.status === undefined ? null : (
              <Section
                title="Применённый переход из обзора"
                description={`Фильтр «${search.status}» сохранён в URL. Таблица подходящих аккаунтов появится в A2.2.`}
              >
                <Badge variant="outline">status:{search.status}</Badge>
              </Section>
            )}

            {data.services.map((service) => (
              <ServiceOverview
                key={service.service}
                service={service}
                spaceSlug={spaceSlug}
              />
            ))}

            <Section
              title="Тренды 7 / 30 дней"
              description="Добавлено, авторизовано, верифицировано и забанено."
            >
              <Empty>
                <EmptyHeader>
                  <EmptyTitle>Исторического ряда ещё нет</EmptyTitle>
                  <EmptyDescription>
                    Текущие агрегаты готовы. Rollup и честные временные графики
                    появятся в A4.6.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            </Section>
          </div>
        )}
      </QueryState>
    </DashboardPage>
  );
}
