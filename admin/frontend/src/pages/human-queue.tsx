import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import {
  fetchHumanQueue,
  type HumanQueueQuery,
} from "@/shared/api/human-queue";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { HumanQueueContent } from "@/shared/components/human-queue/human-queue-content";
import {
  HumanQueueControls,
  HumanQueueUpdatedAt,
} from "@/shared/components/human-queue/human-queue-controls";
import { QueryState } from "@/shared/components/states/query-state";
import { ListPage } from "@/shared/components/templates/templates";
import {
  mergeHumanQueueSearch,
  type HumanQueueSearch,
  type HumanQueueSearchPatch,
} from "@/shared/lib/human-queue-search";
import { Skeleton } from "@/shared/ui/skeleton";

function queueQuery(search: HumanQueueSearch): HumanQueueQuery {
  return {
    limit: 50,
    ...(search.cursor === undefined ? {} : { cursor: search.cursor }),
    ...(search.kind === undefined ? {} : { kind: search.kind }),
    ...(search.service === undefined ? {} : { service: search.service }),
    ...(search.urgency === undefined ? {} : { urgency: search.urgency }),
  };
}

export function HumanQueuePage() {
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/human-queue",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/human-queue",
  });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug/human-queue" });
  const queryValues = queueQuery(search);
  const queue = useQuery({
    queryKey: ["space", spaceSlug, "human-queue", queryValues],
    queryFn: ({ signal }) => fetchHumanQueue(spaceSlug, queryValues, signal),
    placeholderData: (previous) => previous,
    refetchInterval: POLL_INTERVAL.operational,
  });

  const patchSearch = (patch: HumanQueueSearchPatch) => {
    void navigate({
      search: (previous) => mergeHumanQueueSearch(previous, patch),
    });
  };

  return (
    <ListPage
      eyebrow="Пространство"
      title={`Нужен человек · ${spaceSlug}`}
      description="Единая очередь challenges, требований, ручных проверок, drift и карантина. Самые срочные задачи показываются первыми."
      actions={
        queue.data === undefined ? null : (
          <HumanQueueUpdatedAt value={queue.dataUpdatedAt} />
        )
      }
    >
      <HumanQueueControls value={search} onPatch={patchSearch} />
      <QueryState
        query={queue}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Очередь пуста",
          description: "Сейчас нет задач, требующих участия человека.",
        }}
        isEmpty={(data) => data.items.length === 0}
      >
        {(data) => (
          <HumanQueueContent
            data={data}
            spaceSlug={spaceSlug}
            now={queue.dataUpdatedAt}
            selectedKind={search.kind}
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
    </ListPage>
  );
}
