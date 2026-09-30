import {
  useMutation,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { useRef } from "react";

import { AdminApiError } from "./transport";

export interface AdminCommandOptions<Variables, Result> {
  mutationFn: (variables: Variables, idempotencyKey: string) => Promise<Result>;
  affectedQueries: (variables: Variables) => QueryKey[];
  sensitive?: boolean;
}

export function isRevisionConflict(error: unknown): error is AdminApiError {
  return (
    error instanceof AdminApiError &&
    (error.status === 412 || error.body?.error.code === "revision_mismatch")
  );
}

/**
 * Execute a command without optimistic domain state. Concurrent clicks share one
 * idempotency key; success and revision conflicts invalidate only declared reads.
 */
export function useAdminCommand<Variables, Result>(
  options: AdminCommandOptions<Variables, Result>,
) {
  const client = useQueryClient();
  const attemptKey = useRef<string | null>(null);
  const activeCalls = useRef(0);
  return useMutation({
    ...(options.sensitive ? { gcTime: 0 } : {}),
    mutationFn: async (variables: Variables) => {
      attemptKey.current ??= globalThis.crypto.randomUUID();
      const key = attemptKey.current;
      activeCalls.current += 1;
      try {
        return await options.mutationFn(variables, key);
      } finally {
        activeCalls.current -= 1;
        if (activeCalls.current === 0) attemptKey.current = null;
      }
    },
    onSuccess: async (_result, variables) => {
      await Promise.all(
        options
          .affectedQueries(variables)
          .map((queryKey) => client.invalidateQueries({ queryKey })),
      );
    },
    onError: async (error, variables) => {
      if (!isRevisionConflict(error)) return;
      await Promise.all(
        options
          .affectedQueries(variables)
          .map((queryKey) => client.invalidateQueries({ queryKey })),
      );
    },
  });
}
