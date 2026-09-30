import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

const identityId = "00000000-0000-0000-0000-000000000042";

test("human queue keeps filters, expiry guards and quick-action position while polling", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  let requests = 0;
  await page.route(
    "**/api/v1/spaces/demo-large/human-queue**",
    async (route) => {
      requests += 1;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            items: [
              {
                id: `challenge:${identityId}:otp`,
                kind: "challenge",
                identity_id: identityId,
                service: "demo_account",
                external_ref: "attention-0042",
                key: "otp",
                reason: "Открытый challenge: otp",
                urgency: "critical",
                deadline: "2026-01-15T11:59:00Z",
                expired: true,
                occurred_at: "2026-01-15T11:50:00Z",
                action_tab: "verification",
                action_available: false,
                action_unavailable_reason:
                  "Действие станет доступно после A3.2/A3.3.",
                details: { kind: "otp", hint: "***42" },
              },
              {
                id: "challenge:00000000-0000-0000-0000-000000000043:otp-current",
                kind: "challenge",
                identity_id: "00000000-0000-0000-0000-000000000043",
                service: "demo_account",
                external_ref: "attention-0043",
                key: "otp-current",
                reason: "Открытый challenge: otp-current",
                urgency: "high",
                deadline: "2027-01-15T11:59:00Z",
                expired: false,
                occurred_at: "2026-01-15T11:55:00Z",
                action_tab: "verification",
                action_available: true,
                action_unavailable_reason:
                  "Откройте карточку для безопасного действия с проверкой revision.",
                details: { kind: "otp", hint: "***43" },
              },
            ],
            next_cursor: null,
            counts: {
              challenge: 2,
              requirement: 2,
              manual_review: 3,
              drift: 4,
              quarantine: 5,
            },
          },
        }),
      });
    },
  );

  await page.goto("/spaces/demo-large/human-queue?urgency=critical");
  await expect(
    page.getByRole("heading", { name: "Нужен человек · demo-large" }),
  ).toBeVisible();
  await expect(page.getByText("Просрочено", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Открытый challenge: otp", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Карантин", { exact: true })).toBeVisible();
  await expect(page.getByText("5", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Обработать" })).toBeDisabled();
  await page.getByRole("button", { name: "Обработать" }).locator("..").hover();
  await expect(
    page.getByText("Действие станет доступно после A3.2/A3.3."),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: /attention-0042/ }),
  ).toHaveAttribute(
    "href",
    /accounts\/00000000-0000-0000-0000-000000000042\?tab=verification/,
  );

  const filteredRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return (
      url.pathname.endsWith("/human-queue") &&
      url.searchParams.get("service") === "demo_account"
    );
  });
  await page.getByLabel("Сервис").fill("demo_account");
  await filteredRequest;
  await expect(page).toHaveURL(/service=demo_account/);
  await expect(page).toHaveURL(/urgency=critical/);

  const requestsAfterFilter = requests;
  await expect
    .poll(() => requests, { timeout: 20_000 })
    .toBeGreaterThan(requestsAfterFilter);
  await expect(page.getByLabel("Сервис")).toHaveValue("demo_account");
  await page.getByRole("link", { name: "Обработать" }).click();
  await expect(page).toHaveURL(
    /accounts\/00000000-0000-0000-0000-000000000043\?tab=verification/,
  );
  await expect(page.getByRole("alert")).toContainText("account not found");
  await page.goBack();
  await expect(page).toHaveURL(/human-queue\?.*service=demo_account/);
  await expect(page).toHaveURL(/urgency=critical/);
  await expect(page.getByText("Открытый challenge: otp-current")).toBeVisible();
  await expect(page.locator("body")).not.toContainText(
    /ciphertext|answer|secret-password/,
  );
});
