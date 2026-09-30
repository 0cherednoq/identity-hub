import { useQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";

import { fetchServiceDetail } from "@/shared/api/services";
import { ServiceDetailContent } from "@/shared/components/services/service-content";
import { QueryState } from "@/shared/components/states/query-state";
import { DetailPage } from "@/shared/components/templates/templates";
import { Skeleton } from "@/shared/ui/skeleton";

export function ServiceDetailPage() {
  const { serviceName } = useParams({
    from: "/_authenticated/services/$serviceName",
  });
  const service = useQuery({
    queryKey: ["service", serviceName],
    queryFn: ({ signal }) => fetchServiceDetail(serviceName, signal),
  });
  return (
    <DetailPage
      eyebrow="Сервис"
      title={service.data?.title ?? serviceName}
      description="Операции, квоты, схемы, верификации и явно отмеченные возможности."
    >
      <QueryState
        query={service}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Сервис не найден",
          description: "Проверьте имя зарегистрированного Service.",
        }}
        isEmpty={() => false}
      >
        {(data) => <ServiceDetailContent item={data} />}
      </QueryState>
    </DetailPage>
  );
}
