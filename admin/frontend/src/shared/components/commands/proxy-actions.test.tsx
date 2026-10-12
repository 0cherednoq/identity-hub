import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { executeProxyAction } from "@/shared/api/proxy-commands";
import type { ProxyActionResponse } from "@/shared/api/generated/types.gen";

import { ProxyActions } from "./proxy-actions";

vi.mock("@/shared/api/proxy-commands", async (loadOriginal) => {
  const original =
    await loadOriginal<typeof import("@/shared/api/proxy-commands")>();
  return { ...original, executeProxyAction: vi.fn() };
});

const override: ProxyActionResponse = {
  id: "proxy:override",
  kind: "override",
  label: "Исправить статус вручную",
  required_role: "admin",
  allowed_targets: ["alive", "quarantined", "dead"],
  requires_confirmation: true,
  consequence_codes: ["overrides_proxy_health"],
};

function wrapper(client: QueryClient) {
  return function Provider({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

describe("proxy actions", () => {
  beforeEach(() => {
    vi.mocked(executeProxyAction).mockReset();
    vi.mocked(executeProxyAction).mockResolvedValue({
      action: "proxy_override",
      changed: true,
      outcome: "quarantined",
      affected_identity_ids: [],
    });
  });

  it("requires acknowledgement and submits the selected health state", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <ProxyActions
        actions={[override]}
        proxyId="00000000-0000-0000-0000-000000000088"
        spaceSlug="demo-large"
      />,
      { wrapper: wrapper(client) },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Исправить статус вручную" }),
    );
    fireEvent.change(screen.getByLabelText("Статус"), {
      target: { value: "quarantined" },
    });
    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "incident response" },
    });
    fireEvent.click(
      screen.getByLabelText("Я понимаю последствия ручного изменения статуса"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Выполнить" }));

    await waitFor(() => {
      expect(executeProxyAction).toHaveBeenCalledOnce();
    });
    expect(vi.mocked(executeProxyAction).mock.calls[0]?.[0]).toMatchObject({
      target: "quarantined",
      reason: "incident response",
      proxyId: "00000000-0000-0000-0000-000000000088",
    });
  });

  it("disables a proxy only after acknowledgement", async () => {
    const disable: ProxyActionResponse = {
      id: "proxy:disable",
      kind: "disable",
      label: "Выключить",
      required_role: "operator",
      requires_confirmation: true,
      consequence_codes: ["stops_proxy_issuing"],
    };
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <ProxyActions
        actions={[disable]}
        proxyId="00000000-0000-0000-0000-000000000088"
        spaceSlug="demo-large"
      />,
      { wrapper: wrapper(client) },
    );
    fireEvent.click(screen.getByRole("button", { name: "Выключить" }));
    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "provider maintenance" },
    });
    const submit = screen.getByRole("button", { name: "Выполнить" });
    expect(submit).toBeDisabled();
    fireEvent.click(
      screen.getByLabelText("Я понимаю, что прокси перестанет выдаваться"),
    );
    fireEvent.click(submit);

    await waitFor(() => {
      expect(executeProxyAction).toHaveBeenCalledOnce();
    });
    expect(vi.mocked(executeProxyAction).mock.calls[0]?.[0]).toMatchObject({
      action: { kind: "disable" },
      reason: "provider maintenance",
    });
  });
});
