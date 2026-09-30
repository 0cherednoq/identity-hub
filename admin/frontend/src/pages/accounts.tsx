import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearch,
} from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";

import {
  AUTH_STATE_VIEW,
  AVAILABILITY_VIEW,
  DomainStatusBadge,
  LIFECYCLE_VIEW,
  STANDING_VIEW,
} from "@/entities/status/status";
import { fetchAccounts, type AccountQuery } from "@/shared/api/accounts";
import type {
  AccountBulkFilter,
  AccountListItemResponse,
  AccountPageResponse,
} from "@/shared/api/generated/types.gen";
import { fetchPrincipal } from "@/shared/api/auth";
import { fetchSpaceOverview } from "@/shared/api/overview";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import {
  AccountColumnPicker,
  AccountFilters,
  type AccountFilterValue,
} from "@/shared/components/accounts/account-controls";
import {
  AccountListSummary,
  DateCell,
  EligibilityCell,
  LeaseCell,
  ProxyCell,
  TagsCell,
} from "@/shared/components/accounts/account-cells";
import { DataTable } from "@/shared/components/data-table/data-table";
import { QueryState } from "@/shared/components/states/query-state";
import { BulkControls } from "@/shared/components/commands/bulk-controls";
import { ListPage } from "@/shared/components/templates/templates";
import { Button } from "@/shared/ui/button";
import { Badge } from "@/shared/ui/badge";
import type { AccountSearch } from "@/shared/lib/account-search";
import { Skeleton } from "@/shared/ui/skeleton";

const DEFAULT_COLUMNS = [
  "short_id",
  "service",
  "external_ref",
  "statuses",
  "eligible",
  "lease",
  "tier",
  "proxy",
  "tags",
  "created",
  "actions",
] as const;

const COLUMN_OPTIONS = [
  { id: "short_id", label: "Короткий ID" },
  { id: "service", label: "Сервис" },
  { id: "external_ref", label: "External ref" },
  { id: "statuses", label: "Статусы" },
  { id: "eligible", label: "Пригодность" },
  { id: "lease", label: "Аренда" },
  { id: "tier", label: "Уровень" },
  { id: "proxy", label: "Прокси" },
  { id: "tags", label: "Теги и метки" },
  { id: "requirements", label: "Requirements" },
  { id: "last_leased", label: "Последняя аренда" },
  { id: "last_synced", label: "Последняя сверка" },
  { id: "error_score", label: "Error score" },
  { id: "links", label: "Связи" },
  { id: "quota", label: "Квота" },
  { id: "created", label: "Добавлен" },
  { id: "profile", label: "Профиль" },
  { id: "actions", label: "Действия" },
] as const;

function optionalDate(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? undefined : date.toISOString();
}

function apiQuery(search: AccountSearch): AccountQuery {
  const query: AccountQuery = { limit: 50 };
  const assign = <Key extends keyof AccountQuery>(
    key: Key,
    value: AccountQuery[Key] | undefined,
  ) => {
    if (value !== undefined) query[key] = value;
  };
  assign("q", search.q);
  assign("cursor", search.cursor);
  assign("service", search.service);
  assign("lifecycle", search.lifecycle);
  assign("standing", search.standing);
  assign("auth", search.auth);
  assign("availability", search.availability);
  assign("tag", search.tag);
  assign("label", search.label);
  assign("attention", search.attention);
  assign("eligible", search.eligible);
  assign("leased", search.leased);
  assign("has_override", search.hasOverride);
  assign("requirement", search.requirement);
  assign("factor", search.factor);
  assign("factor_state", search.factorState);
  assign("tier", search.tier);
  assign("proxy", search.proxy);
  assign("quota_kind", search.quotaKind);
  assign("linked", search.linked);
  assign("created_from", optionalDate(search.createdFrom));
  assign("created_to", optionalDate(search.createdTo));
  assign("last_leased_from", optionalDate(search.lastLeasedFrom));
  assign("last_leased_to", optionalDate(search.lastLeasedTo));
  assign("synced_from", optionalDate(search.syncedFrom));
  assign("synced_to", optionalDate(search.syncedTo));
  assign("standing_changed_from", optionalDate(search.standingChangedFrom));
  assign("standing_changed_to", optionalDate(search.standingChangedTo));
  assign("sort", search.sort);
  assign("direction", search.direction);
  if (search.status === "attention") assign("attention", true);
  else if (search.status?.includes(":") === true) {
    const [region, state] = search.status.split(":", 2);
    if (
      region === "lifecycle" ||
      region === "standing" ||
      region === "auth" ||
      region === "availability"
    ) {
      assign(region, state);
    }
  }
  return query;
}

function bulkFilter(query: AccountQuery): AccountBulkFilter {
  const filter = { ...query };
  delete filter.cursor;
  delete filter.limit;
  return filter;
}

function accountColumns(
  spaceSlug: string,
): ColumnDef<AccountListItemResponse>[] {
  return [
    { id: "short_id", header: "ID", accessorKey: "short_id" },
    { id: "service", header: "Сервис", accessorKey: "service" },
    { id: "external_ref", header: "External ref", accessorKey: "external_ref" },
    {
      id: "statuses",
      header: "Статусы",
      cell: ({ row }) => (
        <>
          <DomainStatusBadge
            registry={LIFECYCLE_VIEW}
            value={row.original.lifecycle}
          />{" "}
          <DomainStatusBadge
            registry={STANDING_VIEW}
            value={row.original.standing}
          />{" "}
          <DomainStatusBadge
            registry={AUTH_STATE_VIEW}
            value={row.original.auth}
          />{" "}
          <DomainStatusBadge
            registry={AVAILABILITY_VIEW}
            value={row.original.availability}
          />
        </>
      ),
    },
    {
      id: "eligible",
      header: "Пригодность",
      cell: ({ row }) => <EligibilityCell row={row.original} />,
    },
    {
      id: "lease",
      header: "Аренда",
      cell: ({ row }) => <LeaseCell row={row.original} />,
    },
    {
      id: "tier",
      header: "Уровень",
      cell: ({ row }) => row.original.verification_tier ?? "—",
    },
    {
      id: "proxy",
      header: "Прокси",
      cell: ({ row }) => <ProxyCell row={row.original} />,
    },
    {
      id: "tags",
      header: "Теги и метки",
      cell: ({ row }) => <TagsCell row={row.original} />,
    },
    {
      id: "requirements",
      header: "Requirements",
      cell: ({ row }) => row.original.requirements.join(" · ") || "—",
    },
    {
      id: "last_leased",
      header: "Последняя аренда",
      cell: ({ row }) => <DateCell value={row.original.last_leased_at} />,
    },
    {
      id: "last_synced",
      header: "Последняя сверка",
      cell: ({ row }) => <DateCell value={row.original.last_synced_at} />,
    },
    {
      id: "error_score",
      header: "Error score",
      accessorKey: "error_score",
    },
    {
      id: "links",
      header: "Связи",
      cell: ({ row }) =>
        `зависит: ${String(row.original.outgoing_links)} · зависят: ${String(row.original.incoming_links)}`,
    },
    {
      id: "quota",
      header: "Квота",
      cell: ({ row }) =>
        row.original.quotas.length === 0
          ? "—"
          : row.original.quotas
              .map(
                (quota) =>
                  `${quota.kind}: ${String(quota.used)} / ${String(quota.limit)}`,
              )
              .join(" · "),
    },
    {
      id: "created",
      header: "Добавлен",
      cell: ({ row }) => <DateCell value={row.original.created_at} />,
    },
    {
      id: "profile",
      header: "Профиль",
      cell: ({ row }) =>
        Object.entries(row.original.profile)
          .slice(0, 3)
          .map(([key, value]) => `${key}:${String(value)}`)
          .join(" · ") || "—",
    },
    {
      id: "actions",
      header: "Действия",
      cell: ({ row }) => (
        <div className="flex gap-2">
          <Button asChild variant="outline">
            <Link
              to="/spaces/$spaceSlug/accounts/$identityId"
              params={{ identityId: row.original.id, spaceSlug }}
            >
              Открыть
            </Link>
          </Button>
          <Button
            aria-label={`Копировать ID ${row.original.short_id}`}
            type="button"
            variant="outline"
            onClick={() => {
              void navigator.clipboard.writeText(row.original.id);
            }}
          >
            Копировать ID
          </Button>
        </div>
      ),
    },
  ];
}

export function AccountsPage() {
  const principal = useQuery({
    queryKey: ["auth", "principal"],
    queryFn: ({ signal }) => fetchPrincipal(signal),
    staleTime: 60_000,
  });
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/accounts",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/accounts",
  });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug/accounts" });
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const selectedColumns = useMemo(
    () =>
      new Set(search.columns?.split(",").filter(Boolean) ?? DEFAULT_COLUMNS),
    [search.columns],
  );
  const columns = useMemo(
    () =>
      accountColumns(spaceSlug).filter((column) =>
        selectedColumns.has(column.id ?? ""),
      ),
    [selectedColumns, spaceSlug],
  );
  const queryValues = apiQuery(search);
  const query = useQuery({
    queryKey: ["space", spaceSlug, "accounts", queryValues],
    queryFn: ({ signal }) => fetchAccounts(spaceSlug, queryValues, signal),
    placeholderData: (previous) => previous,
    refetchInterval: POLL_INTERVAL.operational,
  });
  const overview = useQuery({
    queryKey: ["space", spaceSlug, "overview", "all"],
    queryFn: ({ signal }) => fetchSpaceOverview(spaceSlug, undefined, signal),
    staleTime: POLL_INTERVAL.reference,
  });
  const services =
    overview.data?.services.map((service) => service.service) ?? [];
  const filterValues: AccountFilterValue = {
    attention:
      search.attention === undefined
        ? undefined
        : search.attention
          ? "true"
          : "false",
    auth: search.auth,
    availability: search.availability,
    createdFrom: search.createdFrom,
    createdTo: search.createdTo,
    eligible:
      search.eligible === undefined
        ? undefined
        : search.eligible
          ? "true"
          : "false",
    factor: search.factor,
    factorState: search.factorState,
    hasOverride:
      search.hasOverride === undefined
        ? undefined
        : search.hasOverride
          ? "true"
          : "false",
    lastLeasedFrom: search.lastLeasedFrom,
    lastLeasedTo: search.lastLeasedTo,
    label: search.label,
    leased:
      search.leased === undefined
        ? undefined
        : search.leased
          ? "true"
          : "false",
    lifecycle: search.lifecycle,
    linked:
      search.linked === undefined
        ? undefined
        : search.linked
          ? "true"
          : "false",
    proxy: search.proxy,
    quotaKind: search.quotaKind,
    q: search.q,
    requirement: search.requirement,
    service: search.service,
    standing: search.standing,
    standingChangedFrom: search.standingChangedFrom,
    standingChangedTo: search.standingChangedTo,
    sort: search.sort,
    direction: search.direction,
    syncedFrom: search.syncedFrom,
    syncedTo: search.syncedTo,
    tag: search.tag,
    tier: search.tier,
  };

  const patchSearch = (patch: Partial<AccountSearch>) => {
    void navigate({
      search: (previous) => {
        const next: AccountSearch = { ...previous };
        delete next.cursor;
        for (const [key, value] of Object.entries(patch)) {
          Object.assign(next, { [key]: value });
        }
        return Object.fromEntries(
          Object.entries(next).filter(([, value]) => value !== undefined),
        );
      },
    });
  };

  return (
    <ListPage
      eyebrow="Пространство"
      title={`Аккаунты · ${spaceSlug}`}
      description="Безопасная страница с keyset pagination. Секреты и lease token не входят в API."
      actions={
        <AccountColumnPicker
          columns={[...COLUMN_OPTIONS]}
          selected={selectedColumns}
          onChange={(next) => {
            patchSearch({ columns: [...next].sort().join(",") });
          }}
        />
      }
    >
      {search.status === undefined ? null : (
        <Badge variant="outline">Фильтр перехода: {search.status}</Badge>
      )}
      <AccountFilters
        value={filterValues}
        services={services}
        onPatch={(patch) => {
          const { attention, eligible, hasOverride, leased, linked, ...rest } =
            patch;
          const translated: Partial<AccountSearch> = { ...rest };
          if ("attention" in patch)
            translated.attention =
              attention === undefined ? undefined : attention === "true";
          if ("eligible" in patch)
            translated.eligible =
              eligible === undefined ? undefined : eligible === "true";
          if ("hasOverride" in patch)
            translated.hasOverride =
              hasOverride === undefined ? undefined : hasOverride === "true";
          if ("leased" in patch)
            translated.leased =
              leased === undefined ? undefined : leased === "true";
          if ("linked" in patch)
            translated.linked =
              linked === undefined ? undefined : linked === "true";
          patchSearch(translated);
        }}
        onReset={() => {
          void navigate({
            search:
              search.columns === undefined ? {} : { columns: search.columns },
          });
        }}
      />
      {principal.data?.role === "viewer" ? null : (
        <BulkControls
          filter={bulkFilter(queryValues)}
          spaceSlug={spaceSlug}
          onComplete={() => {
            void query.refetch();
          }}
        />
      )}
      <QueryState<AccountPageResponse>
        query={query}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Аккаунты не найдены",
          description: "Измените фильтры или строку поиска.",
        }}
        isEmpty={(data) => data.items.length === 0}
      >
        {(data) => (
          <div className="grid gap-4">
            <AccountListSummary
              selected={selected.size}
              total={data.total_label}
            />
            <DataTable
              columns={columns}
              data={data.items}
              getRowId={(row) => row.id}
              selectedRowIds={selected}
              onSelectionChange={setSelected}
            />
            <div className="flex items-center justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={search.cursor === undefined}
                onClick={() => {
                  window.history.back();
                }}
              >
                Назад
              </Button>
              <Button
                type="button"
                disabled={data.next_cursor === null}
                onClick={() => {
                  if (data.next_cursor !== null)
                    patchSearch({ cursor: data.next_cursor });
                }}
              >
                Далее
              </Button>
            </div>
          </div>
        )}
      </QueryState>
    </ListPage>
  );
}
