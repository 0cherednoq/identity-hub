import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { executeIntegrationCommand } from "@/shared/api/integrations";

import {
  IntegrationCreateControls,
  IntegrationRowActions,
} from "./integration-controls";

vi.mock("@/shared/api/integrations", () => ({
  executeIntegrationCommand: vi.fn(),
}));

function subject(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{node}</QueryClientProvider>,
  );
}

describe("integration controls", () => {
  beforeEach(() => {
    vi.mocked(executeIntegrationCommand).mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows a webhook signing value once and clears it on blur", async () => {
    vi.mocked(executeIntegrationCommand).mockResolvedValue({
      action: "create",
      one_time_signing_value: "signing-value-visible-once",
      outcome: "changed",
      overlap_seconds: null,
      resource_id: "00000000-0000-0000-0000-000000000077",
      resource_kind: "webhook_endpoint",
    });
    subject(<IntegrationCreateControls isAdmin spaceSlug="demo-large" />);
    fireEvent.click(screen.getByRole("button", { name: "Webhook" }));
    fireEvent.change(screen.getByLabelText("HTTPS URL"), {
      target: { value: "https://hooks.example.test/events" },
    });
    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "Create operations webhook" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать" }));

    await waitFor(() => {
      expect(
        screen.getByText("signing-value-visible-once"),
      ).toBeInTheDocument();
    });
    const createCall = vi.mocked(executeIntegrationCommand).mock.calls[0];
    expect(createCall?.[0]).toMatchObject({
      kind: "webhook.create",
      body: { url: "https://hooks.example.test/events" },
    });
    expect(createCall?.[1]).toEqual(expect.any(String));
    act(() => {
      window.dispatchEvent(new window.Event("blur"));
    });
    await waitFor(() => {
      expect(
        screen.queryByText("signing-value-visible-once"),
      ).not.toBeInTheDocument();
    });
  });

  it("requires explicit confirmation and preserves rotation overlap", async () => {
    vi.mocked(executeIntegrationCommand).mockResolvedValue({
      action: "rotate",
      one_time_signing_value: "rotated-value",
      outcome: "changed",
      overlap_seconds: 3600,
      resource_id: "00000000-0000-0000-0000-000000000088",
      resource_kind: "webhook_endpoint",
    });
    subject(
      <IntegrationRowActions
        enabled
        id="00000000-0000-0000-0000-000000000088"
        isAdmin
        kind="webhook"
        spaceSlug="demo-large"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Ротация" }));
    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "Scheduled key rotation" },
    });
    const submit = screen.getByRole("button", { name: "Подтвердить" });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Я понимаю последствия"));
    fireEvent.click(submit);

    await waitFor(() => {
      expect(screen.getByText("rotated-value")).toBeInTheDocument();
    });
    const rotateCall = vi.mocked(executeIntegrationCommand).mock.calls[0];
    expect(rotateCall?.[0]).toMatchObject({
      kind: "webhook.rotate",
      overlapSeconds: 3600,
    });
    expect(rotateCall?.[1]).toEqual(expect.any(String));
  });
});
