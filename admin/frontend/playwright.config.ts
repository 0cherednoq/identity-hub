import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "tests/e2e",
  outputDir: "../test-results/playwright",
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}-{platform}{ext}",
  // Polling deliberately pauses in hidden tabs. A single browser worker keeps every
  // page visible and also matches the process-local demo backend's isolation model.
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI
    ? [
        ["line"],
        [
          "html",
          { outputFolder: "../test-results/playwright-report", open: "never" },
        ],
      ]
    : "list",
  use: {
    baseURL: "http://127.0.0.1:41739",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command:
        "uv run --project .. litestar --app identity_hub_admin.testing:app run --host 127.0.0.1 --port 48765",
      url: "http://127.0.0.1:48765/health/live",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: "npm run dev -- --port 41739 --strictPort",
      url: "http://127.0.0.1:41739",
      env: { VITE_BACKEND_ORIGIN: "http://127.0.0.1:48765" },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
