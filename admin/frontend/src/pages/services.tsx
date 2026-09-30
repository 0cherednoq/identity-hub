import { useQuery } from "@tanstack/react-query";

import { fetchServices } from "@/shared/api/services";
import { ServiceCatalogContent } from "@/shared/components/services/service-content";
import { QueryState } from "@/shared/components/states/query-state";
import { ListPage } from "@/shared/components/templates/templates";
import { Skeleton } from "@/shared/ui/skeleton";

export function ServicesPage() {
  const services = useQuery({
    queryKey: ["services"],
    queryFn: ({ signal }) => fetchServices(signal),
  });
  return (
    <ListPage
      eyebrow="Справочник"
      title="Сервисы"
      description="Безопасное описание зарегистрированных платформ, правил и возможностей."
    >
      <QueryState
        query={services}
        skeleton={<Skeleton className="h-80 w-full" />}
        empty={{
          title: "Сервисы не зарегистрированы",
          description: "Подключите хотя бы один Service в Identity Hub.",
        }}
        isEmpty={(data) => data.items.length === 0}
      >
        {(data) => <ServiceCatalogContent items={data.items} />}
      </QueryState>
    </ListPage>
  );
}
