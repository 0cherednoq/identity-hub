import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

test("frontend and backend are connected", async ({ page, request }) => {
  await authenticateAs(page, "viewer");
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Identity Hub" }),
  ).toBeVisible();
  await expect(page.getByText("Large dataset")).toBeVisible();
  await expect(page.getByText("Всего 250")).toBeVisible();

  const response = await request.get("http://127.0.0.1:48765/health/live", {
    headers: { "X-Request-ID": "e2e-smoke-001" },
  });
  expect(response.ok()).toBeTruthy();
  expect(response.headers()["x-request-id"]).toBe("e2e-smoke-001");
});
