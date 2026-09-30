import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const ADMIN_ROLES = ["viewer", "operator", "admin"] as const;
export type AdminRole = (typeof ADMIN_ROLES)[number];

export const E2E_CREDENTIALS = {
  username: "e2e-admin",
  password: "e2e-password",
} as const;

export function bearerToken(role: AdminRole): string {
  return `e2e-${role}-token`;
}

/** Install a real bearer principal for all browser requests before navigation. */
export async function authenticateAs(page: Page, role: AdminRole) {
  await page.setExtraHTTPHeaders({
    Authorization: `Bearer ${bearerToken(role)}`,
  });
}

export async function openSpaceAs(
  page: Page,
  role: AdminRole,
  space = "demo-large",
) {
  await authenticateAs(page, role);
  await page.goto(`/spaces/${space}`);
  await expect(page.getByText(`e2e:${role}`)).toHaveCount(0);
  const title = space === "demo-large" ? "Large dataset" : space;
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
}

export async function callOperatorProbe(
  request: APIRequestContext,
  role: AdminRole,
) {
  return request.post("http://127.0.0.1:48765/api/v1/e2e/operator-probe", {
    headers: { Authorization: `Bearer ${bearerToken(role)}` },
  });
}
