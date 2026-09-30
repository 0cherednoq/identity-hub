import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { Download } from "lucide-react";

import {
  fetchAnalyticsCatalog,
  fetchAnalyticsDashboard,
} from "@/shared/api/analytics";
import { AnalyticsControls } from "@/shared/components/analytics/analytics-controls";
import { AnalyticsDashboard } from "@/shared/components/analytics/analytics-dashboard";
import { QueryState } from "@/shared/components/states/query-state";
import { DashboardPage } from "@/shared/components/templates/templates";
import {
  mergeAnalyticsSearch,
  resolveAnalyticsRange,
  type AnalyticsSearchPatch,
} from "@/shared/lib/analytics-search";
import { Button } from "@/shared/ui/button";
import { Skeleton } from "@/shared/ui/skeleton";

function downloadJson(name: string, value: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function AnalyticsPage() {
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/analytics",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/analytics",
  });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug/analytics" });
  const range = useMemo(() => resolveAnalyticsRange(search), [search]);
  const catalog = useQuery({
    queryKey: ["space", spaceSlug, "analytics", "catalog"],
    queryFn: ({ signal }) => fetchAnalyticsCatalog(spaceSlug, signal),
    staleTime: Number.POSITIVE_INFINITY,
  });
  const query = {
    start: range.start,
    end: range.end,
    bucket: range.bucket,
    dimension: search.dimension ?? "service",
    ...(search.service === undefined ? {} : { service: search.service }),
  };
  const dashboard = useQuery({
    queryKey: ["space", spaceSlug, "analytics", "dashboard", query],
    queryFn: ({ signal }) => {
      if (catalog.data === undefined)
        throw new Error("Analytics catalog is not loaded");
      return fetchAnalyticsDashboard(spaceSlug, catalog.data, query, signal);
    },
    enabled: catalog.data !== undefined,
    placeholderData: (previous) => previous,
    staleTime: 60_000,
  });

  const patchSearch = (patch: AnalyticsSearchPatch) => {
    void navigate({
      search: (previous) => mergeAnalyticsSearch(previous, patch),
    });
  };

  return (
    <DashboardPage
      eyebrow="Пространство"
      title={`Аналитика · ${spaceSlug}`}
      description="Событийные ряды, когорты и производные показатели с предыдущим периодом. Окно, шаг, разрез и сервис воспроизводятся из URL."
      actions={
        dashboard.data === undefined ? null : (
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              downloadJson(`analytics-${spaceSlug}.json`, dashboard.data);
            }}
          >
            <Download aria-hidden="true" /> Данные JSON
          </Button>
        )
      }
    >
      <AnalyticsControls value={search} onPatch={patchSearch} />
      <QueryState
        query={catalog}
        skeleton={<Skeleton className="h-28 w-full" />}
        empty={{
          title: "Каталог аналитики пуст",
          description: "Backend не объявил ни одной доступной метрики.",
        }}
        isEmpty={(value) => value.metrics.length === 0}
      >
        {(catalogData) => (
          <QueryState
            query={dashboard}
            skeleton={
              <div className="grid gap-6">
                <Skeleton className="h-96 w-full" />
                <Skeleton className="h-96 w-full" />
              </div>
            }
            empty={{
              title: "Нет аналитических данных",
              description:
                "Выберите другое окно или дождитесь обработки событий роллапами.",
            }}
            isEmpty={() => false}
          >
            {(data) => <AnalyticsDashboard data={data} catalog={catalogData} />}
          </QueryState>
        )}
      </QueryState>
    </DashboardPage>
  );
}
