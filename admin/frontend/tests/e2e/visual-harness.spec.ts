import { expect, test, type Page } from "@playwright/test";

import { authenticateAs } from "./support/auth";

const CASES = [
  { name: "desktop-light", width: 1440, height: 1000, theme: "light" },
  { name: "desktop-dark", width: 1440, height: 1000, theme: "dark" },
  { name: "mobile-light", width: 390, height: 844, theme: "light" },
  { name: "mobile-dark", width: 390, height: 844, theme: "dark" },
] as const;

async function setTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript({
    content: `localStorage.setItem("ih-admin:theme", ${JSON.stringify(theme)});`,
  });
}

for (const visualCase of CASES) {
  test(`shell is stable at ${visualCase.name}`, async ({ page }) => {
    await page.setViewportSize({
      width: visualCase.width,
      height: visualCase.height,
    });
    await setTheme(page, visualCase.theme);
    await authenticateAs(page, "viewer");
    await page.goto("/spaces/demo-large?q=playwright");
    await expect(page.locator("html")).toHaveAttribute(
      "data-theme",
      visualCase.theme,
    );
    await expect(
      page.getByRole("heading", { name: "Large dataset" }),
    ).toBeVisible();
    await expect(page).toHaveScreenshot(`${visualCase.name}.png`, {
      animations: "disabled",
      fullPage: true,
    });
  });
}
