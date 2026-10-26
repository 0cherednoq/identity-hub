import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  revealAccountSecret,
  updateAccountSecret,
} from "@/shared/api/secret-commands";
import type { SecretDescriptorResponse } from "@/shared/api/generated/types.gen";

import { SecretControls } from "./secret-controls";

vi.mock("@/shared/api/secret-commands", () => ({
  revealAccountSecret: vi.fn(),
  updateAccountSecret: vi.fn(),
}));

const credential: SecretDescriptorResponse = {
  field: "credentials",
  label: "Credentials",
  update_kind: "credentials",
};

function subject(onReload = vi.fn()) {
  return render(
    <SecretControls
      descriptors={[credential]}
      identityId="00000000-0000-0000-0000-000000000042"
      revision={9}
      spaceSlug="demo-large"
      onReload={onReload}
    />,
  );
}

describe("secret controls", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.mocked(revealAccountSecret).mockReset();
    vi.mocked(updateAccountSecret).mockReset();
    vi.mocked(revealAccountSecret).mockResolvedValue({
      field: "credentials",
      value: { username: "alice", password: "visible-secret" },
      hide_after_seconds: 30,
    });
    vi.mocked(updateAccountSecret).mockResolvedValue({
      kind: "credentials",
      changed: true,
    });
  });

  it("keeps reveal outside query state and clears it on blur and remount", async () => {
    const onReload = vi.fn();
    const view = subject(onReload);
    fireEvent.click(screen.getByRole("button", { name: "Показать" }));
    const form = within(
      screen.getByRole("region", { name: "Показать Credentials" }),
    );
    fireEvent.change(form.getByLabelText("Причина"), {
      target: { value: "support request" },
    });
    fireEvent.change(form.getByLabelText("Пароль или текущий admin-токен"), {
      target: { value: "admin-token" },
    });
    fireEvent.click(
      form.getByLabelText("Я подтверждаю доступ к секретному значению"),
    );
    fireEvent.click(form.getByRole("button", { name: "Показать значение" }));

    await waitFor(() => {
      expect(screen.getByTestId("secret-value")).toHaveTextContent(
        "visible-secret",
      );
    });
    expect(onReload).toHaveBeenCalledOnce();
    expect(form.getByLabelText("Пароль или текущий admin-токен")).toHaveValue(
      "",
    );
    act(() => {
      window.dispatchEvent(new window.Event("blur"));
    });
    await waitFor(() => {
      expect(screen.queryByTestId("secret-value")).not.toBeInTheDocument();
    });

    view.unmount();
    subject();
    expect(screen.queryByTestId("secret-value")).not.toBeInTheDocument();
  });

  it("hides the value on blur fired right when it appears", async () => {
    // blur из MutationObserver приходит до пассивных эффектов React: слушатель должен уже быть
    const view = subject();
    const observer = new MutationObserver(() => {
      if (screen.queryByTestId("secret-value") !== null) {
        observer.disconnect();
        window.dispatchEvent(new window.Event("blur"));
      }
    });
    observer.observe(view.container, { childList: true, subtree: true });
    fireEvent.click(screen.getByRole("button", { name: "Показать" }));
    const form = within(
      screen.getByRole("region", { name: "Показать Credentials" }),
    );
    fireEvent.change(form.getByLabelText("Причина"), {
      target: { value: "support request" },
    });
    fireEvent.change(form.getByLabelText("Пароль или текущий admin-токен"), {
      target: { value: "admin-token" },
    });
    fireEvent.click(
      form.getByLabelText("Я подтверждаю доступ к секретному значению"),
    );
    fireEvent.click(form.getByRole("button", { name: "Показать значение" }));

    await waitFor(() => {
      expect(revealAccountSecret).toHaveBeenCalledOnce();
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    observer.disconnect();
    expect(screen.queryByTestId("secret-value")).not.toBeInTheDocument();
  });

  it("parses update JSON and never echoes the replacement", async () => {
    subject();
    fireEvent.click(screen.getByRole("button", { name: "Обновить" }));
    const form = within(
      screen.getByRole("region", { name: "Обновить Credentials" }),
    );
    fireEvent.change(form.getByLabelText("Новое значение (JSON)"), {
      target: { value: '{"username":"alice","password":"replacement"}' },
    });
    fireEvent.change(form.getByLabelText("Причина"), {
      target: { value: "scheduled rotation" },
    });
    fireEvent.change(form.getByLabelText("Пароль или текущий admin-токен"), {
      target: { value: "admin-token" },
    });
    fireEvent.click(
      form.getByLabelText("Я подтверждаю доступ к секретному значению"),
    );
    fireEvent.click(form.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => {
      expect(updateAccountSecret).toHaveBeenCalledOnce();
    });
    expect(vi.mocked(updateAccountSecret).mock.calls[0]?.[0]).toMatchObject({
      kind: "credentials",
      value: { username: "alice", password: "replacement" },
      revision: 9,
    });
    await waitFor(() => {
      expect(
        screen.queryByRole("region", { name: "Обновить Credentials" }),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByText("replacement")).not.toBeInTheDocument();
  });
});
