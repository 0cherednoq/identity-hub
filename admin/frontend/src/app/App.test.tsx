import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchPrincipal } from "@/shared/api/auth";
import { fetchOverview } from "@/shared/api/overview";
import { App } from "./App";
import { AppProviders } from "./providers";
import { adminQueryClient } from "@/shared/api/query-client";

vi.mock("@/shared/api/auth", () => ({
  fetchPrincipal: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}));
vi.mock("@/shared/api/overview", () => ({
  fetchOverview: vi.fn(),
  fetchSpaces: vi.fn(),
  fetchSpaceOverview: vi.fn(),
}));

const principal = {
  actor: "viewer:test",
  role: "viewer",
  allowed_spaces: ["demo-large"],
};

afterEach(() => {
  adminQueryClient.clear();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

function renderApp() {
  render(
    <AppProviders>
      <App />
    </AppProviders>,
  );
}

describe("App", () => {
  it("renders loading and then the scoped operational overview", async () => {
    vi.mocked(fetchPrincipal).mockResolvedValue(principal);
    vi.mocked(fetchOverview).mockResolvedValue({
      service: null,
      totals: {
        spaces: 1,
        total: 250,
        eligible: 215,
        free_eligible: 200,
        leased: 15,
        heavy: 2,
        open_challenges: 1,
        action_required: 3,
      },
      spaces: [
        {
          id: "00000000-0000-0000-0000-000000000001",
          slug: "demo-large",
          title: "Large dataset",
          total: 250,
          eligible: 215,
          free_eligible: 200,
          leased: 15,
          heavy: 2,
          open_challenges: 1,
          action_required: 3,
          added_24h: 4,
          added_7d: 20,
          services: [],
        },
      ],
    });

    renderApp();

    expect(screen.getByLabelText("Загрузка")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Identity Hub" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Large dataset")).toBeInTheDocument();
    expect(screen.getByText("Всего 250")).toBeInTheDocument();
  });

  it("renders a useful connection error", async () => {
    adminQueryClient.setQueryDefaults(["overview"], { retry: false });
    vi.mocked(fetchPrincipal).mockResolvedValue(principal);
    vi.mocked(fetchOverview).mockRejectedValue(new Error("connection refused"));

    renderApp();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Ошибка загрузки",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("connection refused");
  });
});
