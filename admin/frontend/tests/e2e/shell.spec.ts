import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

test("deep link, history and keyboard navigation preserve safe URL state", async ({
  page,
}) => {
  await authenticateAs(page, "operator");
  await page.goto("/spaces/demo-large?status=attention");
  await expect(page.getByText("status:attention")).toBeVisible();
  await page.getByRole("link", { name: "Только этот сервис" }).click();
  await expect(page).toHaveURL(/service=demo_account/);
  await page.goBack();
  await expect(page).toHaveURL(/status=attention/);
  await expect(page).not.toHaveURL(/service=/);

  const userMenu = page.getByRole("button", { name: "Меню пользователя" });
  await userMenu.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("e2e:operator")).toBeVisible();
});
