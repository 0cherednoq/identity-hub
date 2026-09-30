import { QueryClient } from "@tanstack/react-query";

import { AdminApiError, RequestAbortedError } from "./transport";

export const POLL_INTERVAL = {
  operational: 15_000,
  backgroundJobs: 10_000,
  reference: 60_000,
} as const;

export function createAdminQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        refetchIntervalInBackground: false,
        refetchOnWindowFocus: true,
        retry: (failureCount, error) => {
          if (error instanceof RequestAbortedError) return false;
          if (error instanceof AdminApiError && error.status < 500)
            return false;
          return failureCount < 2;
        },
        staleTime: POLL_INTERVAL.operational,
      },
      mutations: { retry: false },
    },
  });
}

export const adminQueryClient = createAdminQueryClient();

export async function invalidateSpaceQueries(
  client: QueryClient,
  spaceSlug: string,
): Promise<void> {
  await client.invalidateQueries({ queryKey: ["space", spaceSlug] });
}

export function clearSessionCache(client: QueryClient): void {
  client.clear();
}
