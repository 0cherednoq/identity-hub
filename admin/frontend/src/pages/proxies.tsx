import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import { fetchProxies, type ProxyListQuery } from "@/shared/api/proxies";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { ProxyControls } from "@/shared/components/proxies/proxy-controls";
import { ProxyOverviewContent } from "@/shared/components/proxies/proxy-content";
import { QueryState } from "@/shared/components/states/query-state";
import { ListPage } from "@/shared/components/templates/templates";
import {
  mergeProxySearch,
  type ProxySearchPatch,
} from "@/shared/lib/proxy-search";
import { Skeleton } from "@/shared/ui/skeleton";

export function ProxiesPage() {
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/proxies",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/proxies",
  });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug/proxies" });
  const query: ProxyListQuery = { limit: 50, ...search };
  const proxies = useQuery({
    queryKey: ["space", spaceSlug, "proxies", query],
    queryFn: ({ signal }) => fetchProxies(spaceSlug, query, signal),
    placeholderData: (previous) => previous,
    refetchInterval: POLL_INTERVAL.operational,
  });
  const patch = (value: ProxySearchPatch) => {
    void navigate({ search: (previous) => mergeProxySearch(previous, value) });
  };
  return (
    <ListPage
      eyebrow="Пространство"
      title={`Прокси · ${spaceSlug}`}
      description="Здоровье, ёмкость, привязки и активные аренды без раскрытия логинов и паролей."
    >
      <ProxyControls value={search} onPatch={patch} />
      <QueryState
        query={proxies}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Прокси не найдены",
          description: "Добавьте прокси или измените фильтры.",
        }}
        isEmpty={(data) => data.items.length === 0}
      >
        {(data) => (
          <ProxyOverviewContent
            data={data}
            spaceSlug={spaceSlug}
            hasPrevious={search.cursor !== undefined}
            onPrevious={() => {
              window.history.back();
            }}
            onNext={(cursor) => {
              patch({ cursor });
            }}
          />
        )}
      </QueryState>
    </ListPage>
  );
}
