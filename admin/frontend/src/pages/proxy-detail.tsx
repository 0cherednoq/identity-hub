import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import { fetchProxyDetail } from "@/shared/api/proxies";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { ProxyDetailContent } from "@/shared/components/proxies/proxy-content";
import { ProxyActions } from "@/shared/components/commands/proxy-actions";
import { QueryState } from "@/shared/components/states/query-state";
import { DetailPage } from "@/shared/components/templates/templates";
import { Skeleton } from "@/shared/ui/skeleton";

export function ProxyDetailPage() {
  const { spaceSlug, proxyId } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/proxies/$proxyId",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/proxies/$proxyId",
  });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug/proxies/$proxyId" });
  const detail = useQuery({
    queryKey: ["space", spaceSlug, "proxy", proxyId, search.historyCursor],
    queryFn: ({ signal }) =>
      fetchProxyDetail(
        spaceSlug,
        proxyId,
        {
          history_limit: 50,
          ...(search.historyCursor === undefined
            ? {}
            : { history_cursor: search.historyCursor }),
        },
        signal,
      ),
    refetchInterval: POLL_INTERVAL.operational,
  });
  return (
    <DetailPage
      eyebrow="Прокси"
      title={detail.data?.proxy.masked_url ?? proxyId}
      description="Карточка прокси, текущие привязки и безопасная история изменений."
    >
      <QueryState
        query={detail}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Прокси не найден",
          description: "Проверьте адрес и доступ к пространству.",
        }}
        isEmpty={() => false}
      >
        {(data) => (
          <div className="grid gap-4">
            <ProxyDetailContent
              data={data}
              spaceSlug={spaceSlug}
              hasPrevious={search.historyCursor !== undefined}
              onPrevious={() => {
                window.history.back();
              }}
              onNext={(historyCursor) => {
                void navigate({ search: { historyCursor } });
              }}
            />
            <ProxyActions
              actions={(data.actions ?? []).filter(
                (action) =>
                  action.kind !==
                  (data.proxy.enabled === false ? "disable" : "enable"),
              )}
              proxyId={proxyId}
              spaceSlug={spaceSlug}
            />
          </div>
        )}
      </QueryState>
    </DetailPage>
  );
}
