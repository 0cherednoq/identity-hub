import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";

import type {
  SpaceOverviewResponse,
  SpacesOverviewResponse,
} from "@/shared/api/generated/types.gen";
import { fetchSpaces } from "@/shared/api/overview";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import {
  DataTable,
  NumberCell,
} from "@/shared/components/data-table/data-table";
import {
  CountWithPercent,
  SpaceLink,
  StateMatrix,
} from "@/shared/components/overview/overview";
import { QueryState } from "@/shared/components/states/query-state";
import { ListPage } from "@/shared/components/templates/templates";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";
import { Skeleton } from "@/shared/ui/skeleton";

const columns: ColumnDef<SpaceOverviewResponse>[] = [
  {
    accessorKey: "title",
    header: "Пространство",
    cell: ({ row }) => <SpaceLink space={row.original} />,
  },
  {
    id: "services",
    header: "Сервисы",
    cell: ({ row }) =>
      row.original.services
        .map((service) => `${service.service} ${String(service.total)}`)
        .join(" · ") || "—",
  },
  {
    accessorKey: "total",
    header: "Всего",
    cell: ({ getValue }) => <NumberCell value={getValue<number>()} />,
  },
  {
    id: "eligible",
    header: "Пригодны",
    cell: ({ row }) => (
      <CountWithPercent
        value={row.original.eligible}
        total={row.original.total}
      />
    ),
  },
  {
    accessorKey: "leased",
    header: "В аренде",
    cell: ({ getValue }) => <NumberCell value={getValue<number>()} />,
  },
  {
    accessorKey: "heavy",
    header: "Тяжёлые",
    cell: ({ getValue }) => <NumberCell value={getValue<number>()} />,
  },
  {
    accessorKey: "open_challenges",
    header: "Challenge",
    cell: ({ getValue }) => <NumberCell value={getValue<number>()} />,
  },
  {
    id: "added",
    header: "Добавлено 24ч / 7д",
    cell: ({ row }) =>
      `${String(row.original.added_24h)} / ${String(row.original.added_7d)}`,
  },
];

export function SpacesPage() {
  const search = useSearch({ from: "/_authenticated/spaces" });
  const navigate = useNavigate({ from: "/spaces" });
  const allQuery = useQuery({
    queryKey: ["spaces", "all"],
    queryFn: ({ signal }) => fetchSpaces(undefined, signal),
    refetchInterval: POLL_INTERVAL.operational,
  });
  const query = useQuery({
    queryKey: ["spaces", search.service ?? "all"],
    queryFn: ({ signal }) => fetchSpaces(search.service, signal),
    refetchInterval: POLL_INTERVAL.operational,
  });
  const serviceNames = Array.from(
    new Set(
      allQuery.data?.spaces.flatMap((space) =>
        space.services.map((item) => item.service),
      ) ?? [],
    ),
  ).sort();

  return (
    <ListPage
      eyebrow="Сравнение"
      title="Пространства"
      description="Текущие распределения статусов и ёмкости по разрешённым пространствам."
      actions={
        <Select
          value={search.service ?? "all"}
          onValueChange={(value) =>
            void navigate({ search: value === "all" ? {} : { service: value } })
          }
        >
          <SelectTrigger aria-label="Сервис" className="w-48">
            <SelectValue placeholder="Все сервисы" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Все сервисы</SelectItem>
            {serviceNames.map((service) => (
              <SelectItem key={service} value={service}>
                {service}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      }
    >
      <QueryState<SpacesOverviewResponse>
        query={query}
        skeleton={<Skeleton className="h-80 w-full" />}
        empty={{
          title: "Пространств нет",
          description:
            "Нет пространств в текущем access scope или выбранном service slice.",
        }}
        isEmpty={(data) => data.spaces.length === 0}
      >
        {(data) => (
          <div className="grid gap-6">
            <DataTable
              columns={columns}
              data={data.spaces}
              getRowId={(row) => row.id}
            />
            {(["standing", "auth"] as const).map((region) => (
              <StateMatrix key={region} spaces={data.spaces} region={region} />
            ))}
          </div>
        )}
      </QueryState>
    </ListPage>
  );
}
