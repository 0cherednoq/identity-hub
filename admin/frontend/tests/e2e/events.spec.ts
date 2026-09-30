import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

const eventId = "00000000-0000-0000-0000-000000000091";
const identityId = "00000000-0000-0000-0000-000000000042";
const proxyId = "00000000-0000-0000-0000-000000000071";
const leaseId = "00000000-0000-0000-0000-000000000081";
const item = {
  event_id: eventId,
  type: "lease.acquired",
  group: "journal",
  severity: "info",
  service: "demo_account",
  identity_id: identityId,
  proxy_id: proxyId,
  lease_id: leaseId,
  sequence: 17,
  actor: "worker-17",
  summary: "pool=workers",
  data: { pool: "workers", password: "[redacted]" },
  external: false,
  occurred_at: "2026-09-27T12:00:00Z",
};

test("event journal keeps catalog filters and opens a redacted deep-linked drawer", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  await page.route("**/api/v1/spaces/demo-large/events?**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          items: [item],
          next_cursor: null,
          groups: ["identity", "journal", "proxy"],
          types: [
            {
              type: "identity.registered",
              group: "identity",
              external: true,
              default_severity: "info",
            },
            {
              type: "lease.acquired",
              group: "journal",
              external: false,
              default_severity: "info",
            },
          ],
        },
      }),
    });
  });
  await page.route(
    `**/api/v1/spaces/demo-large/events/${eventId}`,
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ data: item }),
      });
    },
  );

  await page.goto(
    "/spaces/demo-large/events?group=journal&includeJournal=true",
  );
  await expect(
    page.getByRole("heading", { name: "Журнал событий · demo-large" }),
  ).toBeVisible();
  await expect(page.getByLabel("Включая журнальные")).toBeChecked();
  await expect(page.getByText("lease.acquired")).toBeVisible();
  await expect(page.getByText("worker-17")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "аккаунт", exact: true }),
  ).toHaveAttribute(
    "href",
    /accounts\/00000000-0000-0000-0000-000000000042\?tab=events/,
  );
  await expect(
    page.getByRole("link", { name: "прокси", exact: true }),
  ).toHaveAttribute("href", /proxies\/00000000-0000-0000-0000-000000000071/);

  await page.getByLabel("Actor").fill("worker-17");
  await expect(page).toHaveURL(/actor=worker-17/);
  await expect(page).toHaveURL(/group=journal/);
  await expect(page).toHaveURL(/includeJournal=true/);

  await page.getByRole("button", { name: "Подробнее" }).click();
  await expect(page).toHaveURL(/selected=00000000-0000-0000-0000-000000000091/);
  const drawer = page.getByRole("dialog");
  await expect(
    drawer.getByRole("heading", { name: "lease.acquired" }),
  ).toBeVisible();
  await expect(drawer.getByText("[redacted]", { exact: false })).toBeVisible();
  await expect(drawer).not.toContainText("must-not-leak");
  await expect(
    drawer.getByRole("link", { name: "Открыть аккаунт" }),
  ).toHaveAttribute(
    "href",
    /accounts\/00000000-0000-0000-0000-000000000042\?tab=events/,
  );

  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page).not.toHaveURL(/selected=/);
});
