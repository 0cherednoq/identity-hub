import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

test("lease dashboard keeps URL filters while operational data refreshes", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  let dashboardRequests = 0;
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.endsWith("/leases")) {
      dashboardRequests += 1;
    }
  });

  await page.goto(
    "/spaces/demo-large/leases?groupBy=holder&service=demo_account",
  );
  await expect(
    page.getByText("Метрики по окнам", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "15m" })).toBeVisible();
  await expect(page.getByText(/Разрез за 24 часа/)).toBeVisible();
  await expect(page.getByLabel("Сервис")).toHaveValue("demo_account");
  await expect(page).toHaveURL(/groupBy=holder/);

  const filteredResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname.endsWith("/leases") &&
      url.searchParams.get("pool") === "workers"
    );
  });
  await page.getByLabel("Пул").fill("workers");
  await filteredResponse;
  await expect(page).toHaveURL(/pool=workers/);
  await expect(page.getByText("История пуста.")).toBeVisible();

  const requestsAfterFilter = dashboardRequests;
  await expect
    .poll(() => dashboardRequests, { timeout: 20_000 })
    .toBeGreaterThan(requestsAfterFilter);
  await expect(page.getByLabel("Пул")).toHaveValue("workers");
  await expect(page).toHaveURL(/service=demo_account/);
  await expect(page.locator("body")).not.toContainText(
    /lease_token|ciphertext/,
  );
});
