import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

test("viewer delivery center exposes masks, previews and journals without actions", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  await page.goto("/spaces/demo-large/delivery-center");

  await expect(
    page.getByRole("heading", {
      name: "Уведомления и вебхуки · demo-large",
    }),
  ).toBeVisible();
  await expect(page.getByText("Demo operations")).toBeVisible();
  await expect(page.getByText(/••••7890/)).toBeVisible();
  await expect(page.getByText("Critical identity changes")).toBeVisible();
  await expect(page.getByText("Совпадений за 24 ч")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Добавить интеграцию" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Отключить" })).toHaveCount(0);

  await page.getByRole("tab", { name: "Вебхуки" }).click();
  await expect(page).toHaveURL(/tab=webhooks/);
  await expect(page.getByText("https://hooks.example.test/…")).toBeVisible();
  await expect(
    page.getByText("Журнал webhook-доставок", { exact: false }),
  ).toBeVisible();

  await page.getByRole("tab", { name: "Уведомления" }).click();
  await expect(page).toHaveURL(/tab=notifications/);
  await expect(
    page.getByText("Журнал уведомлений", { exact: false }),
  ).toBeVisible();
  await expect(page.locator("body")).not.toContainText(
    /demo-only-telegram-token|demo-only-webhook-secret|token=demo-secret|private\/demo/i,
  );
});

test("admin can toggle a channel and rotate a webhook key", async ({
  page,
}) => {
  await authenticateAs(page, "admin");
  await page.goto("/spaces/demo-large/delivery-center");

  const channelRow = page
    .getByRole("row")
    .filter({ hasText: "Demo operations" });
  await channelRow.getByRole("button", { name: "Отключить" }).click();
  await channelRow.getByLabel("Причина").fill("E2E delivery control check");
  await channelRow.getByRole("button", { name: "Подтвердить" }).click();
  await expect(channelRow.getByText("disabled")).toBeVisible();

  await channelRow.getByRole("button", { name: "Включить" }).click();
  await channelRow
    .getByLabel("Причина")
    .fill("Restore channel after E2E check");
  await channelRow.getByRole("button", { name: "Подтвердить" }).click();
  await expect(channelRow.getByText("enabled")).toBeVisible();

  await page.getByRole("tab", { name: "Вебхуки" }).click();
  const webhookRow = page
    .getByRole("row")
    .filter({ hasText: "https://hooks.example.test/…" });
  await webhookRow.getByRole("button", { name: "Ротация" }).click();
  await webhookRow.getByLabel("Причина").fill("E2E scheduled key rotation");
  await webhookRow.getByLabel("Я понимаю последствия").check();
  await webhookRow.getByRole("button", { name: "Подтвердить" }).click();
  await expect(webhookRow.locator("code.break-all")).toHaveText(
    /[A-Za-z0-9_-]{32,}/,
  );
  await webhookRow.getByRole("button", { name: "Закрыть" }).click();
  await expect(webhookRow.locator("code.break-all")).toHaveCount(0);
});
