import { useQuery } from "@tanstack/react-query";

import { fetchBackground } from "@/shared/api/background";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { BackgroundContent } from "@/shared/components/background/background-content";
import { QueryState } from "@/shared/components/states/query-state";
import { DashboardPage } from "@/shared/components/templates/templates";
import { Skeleton } from "@/shared/ui/skeleton";

export function BackgroundPage() {
  const snapshot = useQuery({
    queryKey: ["background"],
    queryFn: ({ signal }) => fetchBackground(signal),
    refetchInterval: POLL_INTERVAL.backgroundJobs,
  });
  return (
    <DashboardPage
      eyebrow="Диагностика"
      title="Фоновые работы"
      description="Dispatcher, очереди доставки, maintenance и доступность heartbeat-сигналов."
    >
      <QueryState
        query={snapshot}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Диагностика недоступна",
          description: "Backend не вернул фоновые процессы.",
        }}
        isEmpty={(data) => data.processes.length === 0}
      >
        {(data) => <BackgroundContent data={data} />}
      </QueryState>
    </DashboardPage>
  );
}
