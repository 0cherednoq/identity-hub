import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

const service = {
  name: "demo_exchange",
  title: "Demo Exchange",
  total: 42,
  spaces: [{ space_slug: "demo-large", count: 42 }],
  schemas: [
    { category: "attributes", fields: [] },
    {
      category: "profile",
      fields: [
        {
          name: "country",
          kind: "string",
          required: false,
          sensitive: false,
          filterable: true,
          searchable: false,
        },
      ],
    },
    {
      category: "credentials",
      fields: [
        {
          name: "password",
          kind: "secret",
          required: true,
          sensitive: true,
          filterable: false,
          searchable: false,
        },
      ],
    },
    { category: "session", fields: [] },
  ],
  operations: [
    {
      kind: "future_operation",
      standing: ["good", "limited"],
      needs_auth: true,
      verified: ["email"],
      tiers: ["full"],
      quotas: [
        { limit: 3, window_seconds: 86400, origin: "2000-01-03T00:00:00Z" },
      ],
    },
    {
      kind: "probe",
      standing: ["good"],
      needs_auth: false,
      verified: [],
      tiers: [],
      quotas: [],
    },
  ],
  verifications: [
    {
      name: "email",
      requires: [],
      mode: "auto",
      valid_for_seconds: null,
      max_attempts: null,
      reject_codes: [],
      evidence_required: false,
      pending_timeout_seconds: null,
    },
  ],
  tiers: [{ name: "full", factors: ["email"] }],
  standing_codes: [{ code: "frozen_remote", standing: "frozen" }],
  proxy_policy: {
    supported: false,
    pool: null,
    sticky: null,
    max_identities: null,
    on_proxy_down: null,
  },
  capabilities: {
    authenticator: false,
    probes: false,
    error_classifier: false,
    importers: false,
    importer_count: 0,
    normalizers: [],
    profile_encrypted: false,
    lease_mode: "exclusive",
  },
};

test("service registry renders dynamic declarations and explicit unsupported capabilities", async ({
  page,
}) => {
  await authenticateAs(page, "viewer");
  await page.route("**/api/v1/services", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ data: { items: [service] } }),
    });
  });
  await page.route("**/api/v1/services/demo_exchange", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ data: service }),
    });
  });

  await page.goto("/services");
  await expect(page.getByRole("heading", { name: "Сервисы" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Demo Exchange" })).toBeVisible();
  await page.getByRole("link", { name: "Demo Exchange" }).click();

  await expect(page).toHaveURL(/services\/demo_exchange/);
  await expect(
    page.getByRole("heading", { name: "Demo Exchange" }),
  ).toBeVisible();
  await expect(
    page.getByText("future_operation", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("frozen_remote", { exact: true })).toBeVisible();
  await expect(page.getByText("Сервис не использует прокси.")).toBeVisible();
  await expect(
    page.getByText("Не поддерживается", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /demo-large/ })).toHaveAttribute(
    "href",
    /spaces\/demo-large\/accounts\?service=demo_exchange/,
  );
  await expect(page.getByText("чувствительное", { exact: true })).toBeVisible();
  await expect(page.getByText("secret", { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText(
    /actual-password|SecretStr|callable|function/i,
  );
});
