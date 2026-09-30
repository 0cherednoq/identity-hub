import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

const proxyId = "00000000-0000-0000-0000-000000000071";
const identityId = "00000000-0000-0000-0000-000000000042";
const proxy = {
  id: proxyId,
  pool: "residential-ua",
  scheme: "socks5",
  host: "1.2.3.4",
  port: 1080,
  masked_url: "socks5://1.2.3.4:1080",
  status: "alive",
  error_score: 1.5,
  fail_streak: 2,
  next_check_at: "2026-09-27T12:00:00Z",
  checked_at: "2026-09-27T11:45:00Z",
  status_changed_at: "2026-09-27T11:40:00Z",
  status_source: "health-check",
  pending_observation: null,
  pinned_until: null,
  max_identities: 3,
  bound_count: 2,
  available_capacity: 1,
  capacity_utilization: 2 / 3,
  in_lease_count: 1,
  created_at: "2026-09-20T10:00:00Z",
  actions_available: false,
  actions_unavailable_reason: "Действия станут доступны после A3.4.",
};

test("proxy overview preserves filters, polls health and never renders userinfo", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  let requests = 0;
  await page.route("**/api/v1/spaces/demo-large/proxies?**", async (route) => {
    requests += 1;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          items: [proxy],
          next_cursor: null,
          pools: [
            {
              pool: "residential-ua",
              proxies: 4,
              alive: 3,
              quarantined: 1,
              dead: 0,
              other: 0,
              max_identities: 12,
              bound_count: 8,
              available_capacity: 4,
              utilization: 2 / 3,
              in_lease_count: 2,
            },
          ],
        },
      }),
    });
  });

  await page.goto(
    "/spaces/demo-large/proxies?capacity=available&pool=residential-ua",
  );
  await expect(
    page.getByRole("heading", { name: "Прокси · demo-large" }),
  ).toBeVisible();
  await expect(page.getByText("socks5://1.2.3.4:1080")).toBeVisible();
  await expect(page.getByText("8 / 12 привязок")).toBeVisible();
  await expect(page.getByText("score 1.5 · streak 2")).toBeVisible();
  await expect(page.getByLabel("Пул")).toHaveValue("residential-ua");
  await expect(page.locator("body")).not.toContainText(
    /private-user|private-password|userinfo|ciphertext/i,
  );

  await page.getByLabel("Статус").click();
  await page.getByRole("option", { name: "Alive" }).click();
  await expect(page).toHaveURL(/status=alive/);
  await expect(page).toHaveURL(/capacity=available/);
  await expect(page).toHaveURL(/pool=residential-ua/);

  const requestsAfterFilter = requests;
  await expect
    .poll(() => requests, { timeout: 20_000 })
    .toBeGreaterThan(requestsAfterFilter);
});

test("proxy detail links bindings, paginates history and executes guarded override", async ({
  page,
}) => {
  await authenticateAs(page, "admin");
  let overrideRequests = 0;
  await page.route(
    `**/api/v1/spaces/demo-large/proxies/${proxyId}**`,
    async (route) => {
      if (route.request().method() === "POST") {
        overrideRequests += 1;
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            data: {
              action: "proxy_override",
              changed: true,
              outcome: "quarantined",
              affected_identity_ids: [],
            },
          }),
        });
        return;
      }
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            proxy: {
              ...proxy,
              actions_available: true,
              actions_unavailable_reason: "",
            },
            actions: [
              {
                id: "proxy:override",
                kind: "override",
                label: "Исправить статус вручную",
                required_role: "admin",
                allowed_targets: ["alive", "quarantined", "dead"],
                requires_confirmation: true,
                consequence_codes: ["overrides_proxy_health"],
              },
            ],
            bound_identities: [
              {
                identity_id: identityId,
                service: "demo_account",
                external_ref: "attention-0042",
                bound_at: "2026-09-26T10:00:00Z",
              },
            ],
            history: [
              {
                event_id: "00000000-0000-0000-0000-000000000091",
                identity_id: identityId,
                service: "demo_account",
                external_ref: "attention-0042",
                pool: "residential-ua",
                from_proxy_id: null,
                to_proxy_id: proxyId,
                reason: "initial binding",
                actor: "operator@example.test",
                occurred_at: "2026-09-26T10:00:00Z",
              },
            ],
            next_history_cursor: "history-next",
          },
        }),
      });
    },
  );

  await page.goto(`/spaces/demo-large/proxies/${proxyId}`);
  await expect(
    page.getByRole("heading", { name: "socks5://1.2.3.4:1080" }),
  ).toBeVisible();
  await expect(page.getByText("initial binding")).toBeVisible();
  await expect(
    page.getByRole("link", { name: /attention-0042/ }),
  ).toHaveAttribute(
    "href",
    /accounts\/00000000-0000-0000-0000-000000000042\?tab=network/,
  );
  await page.getByRole("button", { name: "Исправить статус вручную" }).click();
  const form = page.getByRole("region", { name: "Исправить статус вручную" });
  await form.locator("select").selectOption("quarantined");
  await form.getByLabel("Причина").fill("e2e incident quarantine");
  await form
    .getByLabel("Я понимаю последствия ручного изменения статуса")
    .check();
  await form.getByRole("button", { name: "Выполнить" }).click();
  await expect.poll(() => overrideRequests).toBe(1);

  await page.getByRole("button", { name: "Далее" }).click();
  await expect(page).toHaveURL(/historyCursor=history-next/);
  await expect(page.locator("body")).not.toContainText(
    /private-user|private-password|userinfo|ciphertext/i,
  );
});
