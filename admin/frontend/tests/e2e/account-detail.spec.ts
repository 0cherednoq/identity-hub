import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

test("account card loads sections lazily and keeps the active tab in URL", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  let headerRequests = 0;
  page.on("response", (response) => {
    if (/\/accounts\/[0-9a-f-]{36}$/.test(new URL(response.url()).pathname)) {
      headerRequests += 1;
    }
  });

  await page.goto("/spaces/demo-large/accounts");
  await page.getByRole("link", { name: "Открыть" }).first().click();
  await expect(page).toHaveURL(/\/accounts\/[0-9a-f-]{36}$/);
  await expect(page.getByText(/revision \d+/)).toBeVisible();
  await expect(page.getByRole("tab", { name: "Состояние" })).toBeVisible();
  expect(headerRequests).toBe(1);

  await page.getByRole("tab", { name: "Доступы и сессии" }).click();
  await expect(page).toHaveURL(/tab=access/);
  await expect(page.getByText("Значения секретов не загружены")).toBeVisible();
  expect(headerRequests).toBe(1);

  await page.getByRole("tab", { name: "События" }).click();
  await expect(page).toHaveURL(/tab=events/);
  await expect(
    page.getByText("Источник данных ещё не подключён"),
  ).toBeVisible();
  await expect(page.locator("body")).not.toContainText(
    /ciphertext|wrapped_dek|lease_token/,
  );

  await page.reload();
  await expect(page.getByText(/revision \d+/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("tab", { name: "События" })).toHaveAttribute(
    "data-state",
    "active",
  );
});

test("missing or out-of-scope account has an explicit error", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  await page.goto(
    "/spaces/demo-large/accounts/00000000-0000-0000-0000-000000000001",
  );
  await expect(page.getByRole("alert")).toContainText("account not found");
});

test("operator executes a server-declared lifecycle action with confirmation", async ({
  page,
}) => {
  await authenticateAs(page, "operator");
  await page.goto("/spaces/demo-loading/accounts");
  await page.getByRole("link", { name: "Открыть" }).first().click();

  await page.getByRole("button", { name: "Вывести из парка" }).click();
  await expect(page.getByText("removes from lease pool")).toBeVisible();
  await page.getByLabel("Причина").fill("e2e lifecycle retirement");
  await page.getByLabel("Я понимаю перечисленные последствия").check();
  await page.getByRole("button", { name: "Выполнить" }).click();

  await expect(page.getByText("Вернуть в парк", { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText(
    /ciphertext|wrapped_dek|password/,
  );
});

test("admin reveal is ephemeral and browser back does not restore the secret", async ({
  page,
}) => {
  const identityId = "00000000-0000-0000-0000-000000000042";
  const secret = "e2e-visible-secret-never-cache";
  await authenticateAs(page, "admin");

  await page.route(
    `**/api/v1/spaces/demo-large/accounts/${identityId}`,
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            id: identityId,
            short_id: "00000000",
            space: "demo-large",
            service: "demo_account",
            external_ref: "secret-e2e-account",
            lifecycle: "active",
            availability: "available",
            standing: "ok",
            auth: "valid",
            eligible: true,
            blockers: [],
            lease: null,
            revision: 7,
            created_at: "2026-09-27T08:00:00Z",
            updated_at: "2026-09-27T08:00:00Z",
          },
        }),
      });
    },
  );
  await page.route(
    `**/api/v1/spaces/demo-large/accounts/${identityId}/access`,
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            section: "access",
            available: true,
            revision: 7,
            data: { credential: { present: true }, sessions: [] },
            actions: [],
            secrets: [
              {
                field: "credentials",
                label: "Credentials",
                update_kind: "credentials",
              },
            ],
          },
        }),
      });
    },
  );
  await page.route(
    `**/api/v1/spaces/demo-large/accounts/${identityId}/secrets/reveal`,
    async (route) => {
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store, private, max-age=0" },
        body: JSON.stringify({
          data: {
            field: "credentials",
            value: { username: "alice", password: secret },
            hide_after_seconds: 30,
          },
        }),
      });
    },
  );

  await page.goto(`/spaces/demo-large/accounts/${identityId}?tab=access`);
  await page.getByRole("button", { name: "Показать", exact: true }).click();
  const form = page.getByRole("region", { name: "Показать Credentials" });
  await form.getByLabel("Причина").fill("e2e support investigation");
  await form
    .getByLabel("Пароль или текущий admin-токен")
    .fill("e2e-admin-token");
  await form.getByLabel("Я подтверждаю доступ к секретному значению").check();
  await form.getByRole("button", { name: "Показать значение" }).click();
  await expect(page.getByTestId("secret-value")).toContainText(secret);

  await page.goto("/spaces/demo-large");
  await page.goBack();
  await expect(page).toHaveURL(/tab=access/);
  await expect(page.getByText("Credentials", { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText(secret);
  const browserStorage = await page.evaluate(
    "JSON.stringify({local: {...localStorage}, session: {...sessionStorage}, url: location.href})",
  );
  expect(browserStorage).not.toContain(secret);
});
