import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import {
  fetchLeaseDashboard,
  fetchLeaseHistory,
  type LeaseDashboardQuery,
  type LeaseHistoryQuery,
} from "@/shared/api/leases";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import {
  LeaseDashboardContent,
  LeaseHistoryContent,
} from "@/shared/components/leases/lease-dashboard";
import {
  LeaseControls,
  LeaseUpdatedAt,
} from "@/shared/components/leases/lease-controls";
import { QueryState } from "@/shared/components/states/query-state";
import { DashboardPage } from "@/shared/components/templates/templates";
import {
  mergeLeaseSearch,
  type LeaseSearch,
  type LeaseSearchPatch,
} from "@/shared/lib/lease-search";
import { Skeleton } from "@/shared/ui/skeleton";

function dashboardQuery(search: LeaseSearch): LeaseDashboardQuery {
  return {
    group_by: search.groupBy ?? "pool",
    ...(search.pool === undefined ? {} : { pool: search.pool }),
    ...(search.service === undefined ? {} : { service: search.service }),
    ...(search.holder === undefined ? {} : { holder: search.holder }),
  };
}

function historyQuery(search: LeaseSearch): LeaseHistoryQuery {
  return {
    limit: 50,
    ...(search.cursor === undefined ? {} : { cursor: search.cursor }),
    ...(search.pool === undefined ? {} : { pool: search.pool }),
    ...(search.service === undefined ? {} : { service: search.service }),
    ...(search.holder === undefined ? {} : { holder: search.holder }),
  };
}

export function LeasesPage() {
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/leases",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/leases",
  });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug/leases" });
  const dashboardValues = dashboardQuery(search);
  const historyValues = historyQuery(search);
  const dashboard = useQuery({
    queryKey: ["space", spaceSlug, "leases", "dashboard", dashboardValues],
    queryFn: ({ signal }) =>
      fetchLeaseDashboard(spaceSlug, dashboardValues, signal),
    placeholderData: (previous) => previous,
    refetchInterval: POLL_INTERVAL.operational,
  });
  const history = useQuery({
    queryKey: ["space", spaceSlug, "leases", "history", historyValues],
    queryFn: ({ signal }) =>
      fetchLeaseHistory(spaceSlug, historyValues, signal),
    placeholderData: (previous) => previous,
  });

  const patchSearch = (patch: LeaseSearchPatch) => {
    void navigate({
      search: (previous) => mergeLeaseSearch(previous, patch),
    });
  };

  return (
    <DashboardPage
      eyebrow="Пространство"
      title={`Аренды · ${spaceSlug}`}
      description="Оперативная картина обновляется каждые 15 секунд. Фильтры и разрез сохраняются в URL."
      actions={
        dashboard.data === undefined ? null : (
          <LeaseUpdatedAt value={dashboard.data.generated_at} />
        )
      }
    >
      <LeaseControls value={search} onPatch={patchSearch} />

      <QueryState
        query={dashboard}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Нет аренд",
          description: "Оперативные данные появятся после первой аренды.",
        }}
        isEmpty={() => false}
      >
        {(data) => <LeaseDashboardContent data={data} spaceSlug={spaceSlug} />}
      </QueryState>

      <QueryState
        query={history}
        skeleton={<Skeleton className="h-64 w-full" />}
        empty={{
          title: "История пуста",
          description: "Завершённых аренд пока нет.",
        }}
        isEmpty={() => false}
      >
        {(data) => (
          <LeaseHistoryContent
            data={data}
            spaceSlug={spaceSlug}
            hasPrevious={search.cursor !== undefined}
            onPrevious={() => {
              window.history.back();
            }}
            onNext={(cursor) => {
              patchSearch({ cursor });
            }}
          />
        )}
      </QueryState>
    </DashboardPage>
  );
}
