import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import {
  ADMIN_ROLES,
  E2E_CREDENTIALS,
  callOperatorProbe,
  openSpaceAs,
} from "./support/auth";

async function expectNoCriticalAxeViolations(page: Page) {
  const result = await new AxeBuilder({ page }).analyze();
  expect(
    result.violations.filter((violation) => violation.impact === "critical"),
  ).toEqual([]);
}

for (const role of ADMIN_ROLES) {
  test(`${role} helper opens an allowed space`, async ({ page }) => {
    await openSpaceAs(page, role);
    await expectNoCriticalAxeViolations(page);
  });
}

test("login to shell and logout works with the keyboard", async ({ page }) => {
  await page.goto("/spaces/demo-large");
  await expect(page).toHaveURL(/\/login\?redirect=/);

  await page.getByLabel("Имя пользователя").fill(E2E_CREDENTIALS.username);
  await page.getByLabel("Пароль").fill(E2E_CREDENTIALS.password);
  await page.getByRole("button", { name: "Войти" }).focus();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/\/spaces\/demo-large$/, { timeout: 15_000 });
  await expect(
    page.getByRole("heading", { name: "Large dataset" }),
  ).toBeVisible();
  await expectNoCriticalAxeViolations(page);

  const userMenu = page.getByRole("button", { name: "Меню пользователя" });
  await userMenu.focus();
  await page.keyboard.press("Enter");
  const logoutItem = page.getByRole("menuitem", { name: "Выйти" });
  await expect(logoutItem).toBeVisible();
  await page.keyboard.press("End");
  await expect(logoutItem).toBeFocused();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/\/login\?redirect=/);
  await expect(
    page.getByRole("heading", { name: "Вход в Identity Hub" }),
  ).toBeVisible();
  await expectNoCriticalAxeViolations(page);
});

test("viewer has no mutation controls and backend rejects a direct action", async ({
  page,
  request,
}) => {
  await openSpaceAs(page, "viewer");
  await expect(page.locator('[data-admin-action="mutation"]')).toHaveCount(0);

  const denied = await callOperatorProbe(request, "viewer");
  expect(denied.status()).toBe(403);
  const deniedBody = (await denied.json()) as unknown;
  expect(deniedBody).toMatchObject({ error: { code: "access_denied" } });

  const operatorResponse = await callOperatorProbe(request, "operator");
  expect(operatorResponse.status()).toBe(201);
  const adminResponse = await callOperatorProbe(request, "admin");
  expect(adminResponse.status()).toBe(201);
});
