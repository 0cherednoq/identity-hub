import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

test("background health distinguishes states and polls every ten seconds", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  let requests = 0;
  await page.route("**/api/v1/background", async (route) => {
    requests += 1;
    const base = {
      kind: "dispatcher",
      heartbeat_available: true,
      last_attempt_at: "2026-09-27T12:00:00Z",
      last_success_at: "2026-09-27T12:00:00Z",
      lag_events: 0,
      lag_seconds: 0,
      backlog: 0,
      failed: 0,
      next_attempt_at: null,
      last_error: null,
      runbook_hint: "Проверьте supervisor и соединение с БД.",
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          generated_at: "2026-09-27T12:00:01Z",
          processes: [
            { ...base, name: "webhooks", status: "healthy" },
            {
              ...base,
              name: "notifications",
              status: "delayed",
              lag_events: 150,
            },
            {
              ...base,
              name: "maintenance:daily",
              kind: "maintenance",
              status: "retrying",
              failed: 2,
              last_error: "Ошибка скрыта",
            },
            {
              ...base,
              name: "stale-probes",
              kind: "probes",
              status: "stopped",
              heartbeat_available: false,
              last_attempt_at: null,
              last_success_at: null,
            },
          ],
        },
      }),
    });
  });

  await page.goto("/background");
  await expect(
    page.getByRole("heading", { name: "Фоновые работы" }),
  ).toBeVisible();
  await expect(page.getByText("webhooks", { exact: true })).toBeVisible();
  await expect(page.getByText("Heartbeat недоступен")).toBeVisible();
  await expect(page.getByText("Ошибка скрыта")).toBeVisible();
  for (const status of ["healthy", "delayed", "retrying", "stopped"]) {
    await expect(page.getByText(status, { exact: true }).first()).toBeVisible();
  }
  const afterInitial = requests;
  await expect
    .poll(() => requests, { timeout: 15_000 })
    .toBeGreaterThan(afterInitial);
  await expect(page.locator("body")).not.toContainText(
    /token|password|ciphertext/i,
  );
});
