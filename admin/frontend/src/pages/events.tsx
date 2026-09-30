import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import {
  fetchEventDetail,
  fetchEvents,
  type EventListQuery,
} from "@/shared/api/events";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { fetchPrincipal } from "@/shared/api/auth";
import type { EventExportFilter } from "@/shared/api/generated/types.gen";
import { EventExportControls } from "@/shared/components/commands/event-export-controls";
import { EventControls } from "@/shared/components/events/event-controls";
import { EventJournalContent } from "@/shared/components/events/event-content";
import { EventDrawer } from "@/shared/components/events/event-drawer";
import { QueryState } from "@/shared/components/states/query-state";
import { ListPage } from "@/shared/components/templates/templates";
import {
  mergeEventSearch,
  type EventSearch,
  type EventSearchPatch,
} from "@/shared/lib/event-search";
import { Skeleton } from "@/shared/ui/skeleton";

function listQuery(search: EventSearch): EventListQuery {
  return {
    limit: 50,
    ...(search.type === undefined ? {} : { type: search.type }),
    ...(search.group === undefined ? {} : { group: search.group }),
    ...(search.severity === undefined ? {} : { severity: search.severity }),
    ...(search.service === undefined ? {} : { service: search.service }),
    ...(search.identityId === undefined
      ? {}
      : { identity_id: search.identityId }),
    ...(search.proxyId === undefined ? {} : { proxy_id: search.proxyId }),
    ...(search.leaseId === undefined ? {} : { lease_id: search.leaseId }),
    ...(search.actor === undefined ? {} : { actor: search.actor }),
    ...(search.occurredFrom === undefined
      ? {}
      : { occurred_from: search.occurredFrom }),
    ...(search.occurredTo === undefined
      ? {}
      : { occurred_to: search.occurredTo }),
    ...(search.includeJournal === undefined
      ? {}
      : { include_journal: search.includeJournal }),
    ...(search.cursor === undefined ? {} : { cursor: search.cursor }),
  };
}

function exportFilter(query: EventListQuery): EventExportFilter {
  const value = { ...query };
  delete value.cursor;
  delete value.limit;
  return value;
}

export function EventsPage() {
  const principal = useQuery({
    queryKey: ["auth", "principal"],
    queryFn: ({ signal }) => fetchPrincipal(signal),
    staleTime: 60_000,
  });
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/events",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/events",
  });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug/events" });
  const query = listQuery(search);
  const events = useQuery({
    queryKey: ["space", spaceSlug, "events", query],
    queryFn: ({ signal }) => fetchEvents(spaceSlug, query, signal),
    placeholderData: (previous) => previous,
    refetchInterval: POLL_INTERVAL.operational,
  });
  const detail = useQuery({
    queryKey: ["space", spaceSlug, "event", search.selected],
    queryFn: ({ signal }) =>
      fetchEventDetail(spaceSlug, search.selected ?? "", signal),
    enabled: search.selected !== undefined,
  });
  const patch = (value: EventSearchPatch) => {
    void navigate({ search: (previous) => mergeEventSearch(previous, value) });
  };
  const closeDrawer = () => {
    patch({ selected: undefined });
  };

  return (
    <ListPage
      eyebrow="Пространство"
      title={`Журнал событий · ${spaceSlug}`}
      description="Каталожные фильтры, устойчивый cursor и безопасные данные без секретов."
    >
      <EventControls
        value={search}
        groups={events.data?.groups ?? []}
        types={events.data?.types ?? []}
        onPatch={patch}
      />
      {principal.data?.role === "viewer" ? null : (
        <EventExportControls
          filter={exportFilter(query)}
          spaceSlug={spaceSlug}
        />
      )}
      <QueryState
        query={events}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Событий нет",
          description: "Измените фильтры или диапазон дат.",
        }}
        isEmpty={(data) => data.items.length === 0}
      >
        {(data) => (
          <EventJournalContent
            data={data}
            spaceSlug={spaceSlug}
            selected={search.selected}
            hasPrevious={search.cursor !== undefined}
            onSelect={(selected) => {
              patch({ selected });
            }}
            onPrevious={() => {
              window.history.back();
            }}
            onNext={(cursor) => {
              patch({ cursor });
            }}
          />
        )}
      </QueryState>
      {search.selected === undefined ? null : (
        <QueryState
          query={detail}
          skeleton={
            <Skeleton className="fixed inset-y-0 right-0 z-50 h-full w-full max-w-2xl" />
          }
          empty={{
            title: "Событие не найдено",
            description: "Оно удалено или недоступно.",
          }}
          isEmpty={() => false}
        >
          {(item) => (
            <EventDrawer
              item={item}
              spaceSlug={spaceSlug}
              onClose={closeDrawer}
            />
          )}
        </QueryState>
      )}
    </ListPage>
  );
}
