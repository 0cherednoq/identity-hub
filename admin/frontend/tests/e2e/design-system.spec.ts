import { expect, test } from "@playwright/test";

for (const theme of ["light", "dark"] as const) {
  test(`design catalog is stable in ${theme} theme`, async ({ page }) => {
    await page.goto(`/_design?theme=${theme}&visual=1`);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await expect(
      page.getByRole("heading", { name: "Дизайн-система" }),
    ).toBeVisible();
    await expect(page.getByText("Неизвестно: backend_new_state")).toBeVisible();
    await expect(page).toHaveScreenshot(`design-${theme}.png`, {
      fullPage: true,
      animations: "disabled",
      maxDiffPixelRatio: 0.03,
    });
  });
}
