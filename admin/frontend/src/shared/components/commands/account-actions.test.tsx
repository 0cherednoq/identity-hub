import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { executeAccountAction } from "@/shared/api/account-commands";
import type { AccountActionResponse } from "@/shared/api/generated/types.gen";

import { AccountActions } from "./account-actions";

vi.mock("@/shared/api/account-commands", async (loadOriginal) => {
  const original =
    await loadOriginal<typeof import("@/shared/api/account-commands")>();
  return { ...original, executeAccountAction: vi.fn() };
});

const retirement: AccountActionResponse = {
  id: "lifecycle:retired",
  kind: "lifecycle",
  label: "Вывести из парка",
  required_role: "operator",
  region: "lifecycle",
  target: "retired",
  requires_confirmation: true,
  consequence_codes: ["removes_from_lease_pool"],
};

const challenge: AccountActionResponse = {
  id: "answer-challenge:00000000-0000-0000-0000-000000000099",
  kind: "answer_challenge",
  label: "Ответить на challenge",
  required_role: "operator",
  region: "auth",
  key: "00000000-0000-0000-0000-000000000099",
};

const link: AccountActionResponse = {
  id: "link:create",
  kind: "link",
  label: "Добавить связь",
  required_role: "operator",
  requires_role_input: true,
};

function wrapper(client: QueryClient) {
  return function Provider({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

describe("account actions", () => {
  beforeEach(() => {
    vi.mocked(executeAccountAction).mockReset();
    vi.mocked(executeAccountAction).mockResolvedValue({
      action: "lifecycle",
      changed: true,
      outcome: "applied",
      region: "lifecycle",
      state: "retired",
    });
  });

  it("requires reason and consequence acknowledgement and sends the current revision", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <AccountActions
        actions={[retirement]}
        identityId="00000000-0000-0000-0000-000000000042"
        revision={17}
        spaceSlug="demo-large"
        onReload={vi.fn()}
      />,
      { wrapper: wrapper(client) },
    );

    fireEvent.click(screen.getByRole("button", { name: "Вывести из парка" }));
    expect(screen.getByText("removes from lease pool")).toBeVisible();
    const submit = screen.getByRole("button", { name: "Выполнить" });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "support retirement" },
    });
    fireEvent.click(
      screen.getByLabelText("Я понимаю перечисленные последствия"),
    );
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => {
      expect(executeAccountAction).toHaveBeenCalledOnce();
    });
    expect(vi.mocked(executeAccountAction).mock.calls[0]?.[0]).toMatchObject({
      identityId: "00000000-0000-0000-0000-000000000042",
      reason: "support retirement",
      revision: 17,
      spaceSlug: "demo-large",
    });
    expect(vi.mocked(executeAccountAction).mock.calls[0]?.[1]).toEqual(
      expect.any(String),
    );
  });

  it("submits a challenge answer as a sensitive value and clears the form", async () => {
    vi.mocked(executeAccountAction).mockResolvedValueOnce({
      action: "answer_challenge",
      changed: true,
      outcome: "succeeded",
      region: "auth",
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <AccountActions
        actions={[challenge]}
        identityId="00000000-0000-0000-0000-000000000042"
        revision={21}
        spaceSlug="demo-large"
        onReload={vi.fn()}
      />,
      { wrapper: wrapper(client) },
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Ответить на challenge" }),
    );
    const form = within(
      screen.getByRole("region", { name: "Ответить на challenge" }),
    );
    const answerInput = document.querySelector<HTMLInputElement>(
      'input[type="password"]',
    );
    expect(answerInput).not.toBeNull();
    if (answerInput === null) throw new Error("challenge answer input missing");
    fireEvent.change(answerInput, { target: { value: "secret-otp" } });
    fireEvent.change(form.getByLabelText("Причина"), {
      target: { value: "operator supplied one-time code" },
    });
    fireEvent.click(form.getByRole("button", { name: "Выполнить" }));

    await waitFor(() => {
      expect(executeAccountAction).toHaveBeenCalledOnce();
    });
    expect(vi.mocked(executeAccountAction).mock.calls[0]?.[0]).toMatchObject({
      answer: "secret-otp",
      revision: 21,
    });
    await waitFor(() => {
      expect(screen.queryByLabelText("Ответ")).not.toBeInTheDocument();
    });
    expect(client.getMutationCache().getAll()).toHaveLength(0);
  });

  it("requires a role and target account for a new link", async () => {
    vi.mocked(executeAccountAction).mockResolvedValueOnce({
      action: "link",
      changed: true,
      outcome: "linked",
      affected_identity_ids: [],
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <AccountActions
        actions={[link]}
        identityId="00000000-0000-0000-0000-000000000042"
        revision={24}
        spaceSlug="demo-large"
        onReload={vi.fn()}
      />,
      { wrapper: wrapper(client) },
    );
    fireEvent.click(screen.getByRole("button", { name: "Добавить связь" }));
    fireEvent.change(screen.getByLabelText("Роль связи"), {
      target: { value: "recovery_email" },
    });
    fireEvent.change(screen.getByLabelText("UUID связанного аккаунта"), {
      target: { value: "00000000-0000-0000-0000-000000000077" },
    });
    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "new recovery path" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Выполнить" }));

    await waitFor(() => {
      expect(executeAccountAction).toHaveBeenCalledOnce();
    });
    expect(vi.mocked(executeAccountAction).mock.calls[0]?.[0]).toMatchObject({
      role: "recovery_email",
      target: "00000000-0000-0000-0000-000000000077",
      revision: 24,
    });
  });
});
