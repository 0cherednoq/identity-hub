import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandConflictDialog } from "@/shared/components/commands/command-conflict-dialog";

import { useAdminCommand } from "./commands";
import { AdminApiError } from "./transport";

function wrapper(client: QueryClient) {
  return function TestQueryProvider({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

describe("admin command protocol", () => {
  it("shares an idempotency key across concurrent clicks and invalidates targeted reads", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const keys: string[] = [];
    let release: (() => void) | undefined;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const mutationFn = vi.fn(
      async (_value: { reason: string }, key: string) => {
        keys.push(key);
        await blocked;
        return { ok: true };
      },
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(
      () =>
        useAdminCommand({
          mutationFn,
          affectedQueries: () => [["space", "alpha", "account", "one"]],
        }),
      { wrapper: wrapper(client) },
    );

    act(() => {
      result.current.mutate({ reason: "support request" });
      result.current.mutate({ reason: "support request" });
    });
    await waitFor(() => {
      expect(mutationFn).toHaveBeenCalledTimes(2);
    });
    expect(keys[0]).toBe(keys[1]);
    release?.();
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalled();
    });
  });

  it("shows fresh revisions while preserving the entered reason", () => {
    const error = new AdminApiError(412, {
      error: {
        code: "revision_mismatch",
        message: "object revision is stale",
        request_id: "request-1",
        details: { expected_revision: 4, current_revision: 5 },
      },
    });
    render(
      <CommandConflictDialog
        error={error}
        reason="customer ticket 42"
        onClose={vi.fn()}
        onReload={vi.fn()}
      />,
    );

    expect(screen.getByRole("alertdialog")).toHaveTextContent("4");
    expect(screen.getByRole("alertdialog")).toHaveTextContent("5");
    expect(screen.getByRole("alertdialog")).toHaveTextContent(
      "customer ticket 42",
    );
  });
});
