import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { fetchOverview } from "@/shared/api/overview";
import type { SpacesOverviewResponse } from "@/shared/api/generated/types.gen";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { Section } from "@/shared/components/layout/layout";
import {
  OverviewMetrics,
  SpaceSummaryList,
} from "@/shared/components/overview/overview";
import { QueryState } from "@/shared/components/states/query-state";
import { DashboardPage } from "@/shared/components/templates/templates";
import { Button } from "@/shared/ui/button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/shared/ui/empty";
import { Skeleton } from "@/shared/ui/skeleton";

export function HomePage() {
  const query = useQuery({
    queryKey: ["overview"],
    queryFn: ({ signal }) => fetchOverview(undefined, signal),
    refetchInterval: POLL_INTERVAL.operational,
  });

  return (
    <DashboardPage
      eyebrow="Операционный обзор"
      title="Identity Hub"
      description="Текущий срез только по доступным пространствам. Исторические ряды не подменяются текущими значениями."
      actions={
        <Button asChild variant="outline">
          <Link to="/spaces" search={{}}>
            Все пространства
          </Link>
        </Button>
      }
    >
      <QueryState<SpacesOverviewResponse>
        query={query}
        skeleton={
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }, (_, index) => (
              <Skeleton className="h-28" key={index} />
            ))}
          </div>
        }
        empty={{
          title: "Нет доступных пространств",
          description: "Проверьте allowed_spaces текущей учётной записи.",
        }}
        isEmpty={(data) => data.spaces.length === 0}
      >
        {(data) => (
          <div className="grid gap-6">
            <OverviewMetrics totals={data.totals} />
            <Section
              title="Пространства"
              description="Компактный текущий срез; название открывает dashboard пространства."
            >
              <SpaceSummaryList spaces={data.spaces} />
            </Section>
            <Section
              title="Тренды"
              description="Добавления, авторизации, верификации и баны за 7/30 дней."
            >
              <Empty>
                <EmptyHeader>
                  <EmptyTitle>История пока недоступна</EmptyTitle>
                  <EmptyDescription>
                    Текущие значения не выдаются за временной ряд. Rollup и
                    графики появятся в A4.6.
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
