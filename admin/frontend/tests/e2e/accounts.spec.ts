import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

test("account filters, keyset history and selection survive normal navigation", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  await page.goto("/spaces/demo-large/accounts?lifecycle=ready");

  await expect(
    page.getByRole("heading", { name: "Аккаунты · demo-large" }),
  ).toBeVisible();
  await expect(page.getByText("Найдено: 215")).toBeVisible();
  await expect(page.locator("tbody tr")).toHaveCount(50);

  const firstPageRef = await page
    .locator("tbody tr")
    .first()
    .locator("td")
    .nth(3)
    .innerText();
  await page.getByRole("button", { name: "Далее" }).click();
  await expect(page).toHaveURL(/cursor=/);
  await expect(page.locator("tbody tr").first()).not.toContainText(
    firstPageRef,
  );

  await page.getByRole("button", { name: "Назад" }).click();
  await expect(page).not.toHaveURL(/cursor=/);
  await expect(page.locator("tbody tr").first()).toContainText(firstPageRef);

  await page.getByLabel("Выбрать строку", { exact: false }).first().check();
  await expect(page.getByText("Выбрано: 1")).toBeVisible();
  await expect(page.getByText(/мутации отключены/)).toBeVisible();
});

test("account URL presets remain reproducible after reload", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  await page.goto("/spaces/demo-large/accounts");

  await page.getByRole("button", { name: "Пригодные" }).click();
  await expect(page).toHaveURL(/eligible=true/);
  await page.reload();
  await expect(page.getByLabel("Пригоден")).toContainText("Да");

  await page.getByRole("button", { name: "Сбросить" }).click();
  const search = page.getByLabel("Поиск аккаунтов");
  await search.fill("demo-large-0001");
  await page.getByRole("button", { name: "Найти" }).click();
  await expect(page).toHaveURL(/q=demo-large-0001/);
  await page.reload();
  await expect(search).toHaveValue("demo-large-0001");
  await expect(
    page.getByText("demo-large-0001", { exact: true }),
  ).toBeVisible();
});

test("overview drill-down applies its region filter", async ({ page }) => {
  await authenticateAs(page, "viewer");
  await page.goto(
    "/spaces/demo-large/accounts?status=lifecycle%3Aprovisioning",
  );

  await expect(
    page.getByText("Фильтр перехода: lifecycle:provisioning"),
  ).toBeVisible();
  await expect(page.getByText("Найдено: 35")).toBeVisible();
});

test("server sorting is stable and stored in the URL", async ({ page }) => {
  await authenticateAs(page, "viewer");
  await page.goto("/spaces/demo-large/accounts");

  await page.getByLabel("Сортировка").click();
  await page.getByRole("option", { name: "External ref" }).click();
  await page.getByLabel("Направление").click();
  await page.getByRole("option", { name: "По возрастанию" }).click();

  await expect(page).toHaveURL(/sort=external_ref/);
  await expect(page).toHaveURL(/direction=asc/);
  await expect(page.locator("tbody tr").first()).toContainText(
    "demo-large-0001",
  );
  await page.getByRole("button", { name: "Далее" }).click();
  await expect(page.locator("tbody tr").first()).toContainText(
    "demo-large-0051",
  );
  await page.reload();
  await expect(page.locator("tbody tr").first()).toContainText(
    "demo-large-0051",
  );
});

test("operator previews a frozen filter before confirming a bulk action", async ({
  page,
}) => {
  await authenticateAs(page, "operator");
  let previewBody: Record<string, unknown> | null = null;
  let executeRequests = 0;
  await page.route("**/api/v1/spaces/demo-large/exports", async (route) => {
    await route.fulfill({
      status: 201,
      contentType: "text/csv; charset=utf-8",
      body: "id,external_ref\r\n1,safe\r\n",
    });
  });
  await page.route(
    "**/api/v1/spaces/demo-large/bulk/preview",
    async (route) => {
      previewBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            snapshot_id: "00000000-0000-0000-0000-000000003699",
            operation: "tag_add",
            selected_count: 35,
            truncated: false,
            expires_at: "2026-09-27T12:15:00Z",
            sample: [
              {
                identity_id: "00000000-0000-0000-0000-000000003601",
                service: "demo_account",
                external_ref: "preview-account",
                revision: 4,
              },
            ],
          },
        }),
      });
    },
  );
  await page.route(
    "**/api/v1/spaces/demo-large/bulk/execute",
    async (route) => {
      executeRequests += 1;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            snapshot_id: "00000000-0000-0000-0000-000000003699",
            operation: "tag_add",
            total: 35,
            succeeded: 35,
            failed: 0,
            skipped_already_succeeded: 0,
            attempt: 1,
            complete: true,
            failures: [],
          },
        }),
      });
    },
  );

  await page.goto(
    "/spaces/demo-large/accounts?lifecycle=provisioning&tag=demo",
  );
  await page.getByLabel("Причина").fill("e2e scheduled batch");
  await page.getByRole("button", { name: "Preview batch" }).click();
  await expect(page.getByText(/Зафиксировано: 35/)).toBeVisible();
  expect(previewBody).toMatchObject({
    filter: { lifecycle: "provisioning", tag: "demo" },
    operation: { kind: "tag_add", tags: ["reviewed"] },
  });
  await page
    .getByLabel("Я подтверждаю изменение зафиксированного набора аккаунтов")
    .check();
  await page.getByRole("button", { name: "Выполнить batch" }).click();
  await expect(page.getByText(/Batch attempt 1:/)).toContainText(
    "успешно 35 / 35",
  );
  expect(executeRequests).toBe(1);

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Экспорт CSV" }).click();
  await expect
    .poll(async () => (await downloadPromise).suggestedFilename())
    .toBe("accounts.csv");
});
