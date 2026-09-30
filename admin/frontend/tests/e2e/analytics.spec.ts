import { expect, test, type Page, type Route } from "@playwright/test";

import { authenticateAs } from "./support/auth";

const LONG_GROUPS = Array.from(
  { length: 10 },
  (_, index) => `service-with-a-deliberately-long-legend-${String(index + 1)}`,
);

function envelope(data: unknown) {
  return { data };
}

async function analyticsFixture(route: Route) {
  const url = new URL(route.request().url());
  const path = url.pathname;
  if (path.endsWith("/analytics/catalog")) {
    await route.fulfill({
      json: envelope({
        timezone: "Europe/Berlin",
        bucket_sizes: ["1m", "1h", "1d"],
        metrics: [
          "identities.added",
          "auth.authenticated",
          "verifications.verified",
          "tiers.reached",
          "standings.entered",
          "leases.acquired",
          "state.drift",
          "proxies.status",
        ].map((metric) => ({
          metric,
          dimensions: ["service", "state", "factor", "pool"],
        })),
        formulas: {
          series: "Сумма событий в календарной корзине; пропуски равны нулю.",
          funnel: "Добавлено → авторизовано → уровень.",
          time_to: "Время от добавления до первого результата.",
          cohorts: "Доля good среди наблюдаемых аккаунтов когорты.",
          ban_rate: "Забанено / добавлено.",
          utilization: "Пересечение аренд / пригодное время.",
          proxy_quality: "Ошибки с виной прокси и переходы здоровья.",
        },
        max_points: 1000,
        max_groups: 50,
        max_range_days: 366,
      }),
    });
    return;
  }
  if (path.endsWith("/analytics/series")) {
    const metric = url.searchParams.get("metric") ?? "unknown";
    const names = metric === "identities.added" ? LONG_GROUPS : ["Все"];
    await route.fulfill({
      json: envelope({
        metric,
        bucket_size: "1d",
        group_by: metric === "identities.added" ? "service" : null,
        groups: names.map((key, index) => ({
          key,
          total: 30 + index,
          previous_total: 25 + index,
          points: [
            { at: "2026-01-01T00:00:00Z", value: 10 + index },
            { at: "2026-01-02T00:00:00Z", value: 20 + index },
          ],
        })),
        coverage: metric === "state.drift" ? "partial" : "complete",
        cache_tag: `etag-${metric}`,
      }),
    });
    return;
  }
  if (path.endsWith("/analytics/funnel")) {
    await route.fulfill({
      json: envelope({
        stages: [
          { key: "added", count: 100, share: 1 },
          { key: "authenticated", count: 75, share: 0.75 },
          { key: "tier:basic", count: 50, share: 0.5 },
        ],
        cache_tag: "funnel",
      }),
    });
    return;
  }
  if (path.endsWith("/analytics/time-to")) {
    await route.fulfill({
      json: envelope({
        metric: "added_to_authenticated",
        samples: 75,
        median_seconds: 90,
        p90_seconds: 300,
        average_attempts: null,
        cache_tag: "time",
      }),
    });
    return;
  }
  if (path.endsWith("/analytics/cohorts")) {
    await route.fulfill({
      json: envelope({
        rows: [
          {
            cohort_start: "2026-01-05T00:00:00Z",
            size: 100,
            cells: [1, 7, 14, 30].map((offset_days) => ({
              offset_days,
              eligible: 100,
              good: 90 - offset_days,
              share: (90 - offset_days) / 100,
            })),
          },
        ],
        coverage: "partial",
        cache_tag: "cohorts",
      }),
    });
    return;
  }
  if (path.endsWith("/analytics/ban-rate")) {
    await route.fulfill({
      json: envelope({
        group_by: "service",
        items: [{ key: "demo_account", added: 100, banned: 8, rate: 0.08 }],
        cache_tag: "ban",
      }),
    });
    return;
  }
  if (path.endsWith("/analytics/utilization")) {
    await route.fulfill({
      json: envelope({
        leased_seconds: 3600,
        eligible_current: 100,
        utilization: 0.5,
        heatmap: [
          { weekday: 0, hour: 12, leases: 12, operations: 40, failed: 2 },
        ],
        cache_tag: "utilization",
      }),
    });
    return;
  }
  if (path.endsWith("/analytics/proxy-quality")) {
    await route.fulfill({
      json: envelope({
        failures: 4,
        quarantined: 2,
        dead: 1,
        recovered: 3,
        cache_tag: "proxy",
      }),
    });
    return;
  }
  await route.fallback();
}

async function openAnalytics(page: Page) {
  await authenticateAs(page, "viewer");
  await page.route(
    "**/api/v1/spaces/demo-large/analytics/**",
    analyticsFixture,
  );
  await page.goto(
    "/spaces/demo-large/analytics?window=30d&bucket=1d&dimension=service&service=demo_account",
  );
  await expect(
    page.getByRole("heading", { name: "Аналитика · demo-large" }),
  ).toBeVisible();
}

test("analytics reproduces URL state and every chart has a table and safe exports", async ({
  page,
}) => {
  await openAnalytics(page);
  await expect(page).toHaveURL(/window=30d/);
  await expect(page.getByLabel("Сервис аналитики")).toHaveValue("demo_account");
  await expect(page.getByText("Таймзона: Europe/Berlin")).toBeVisible();
  await expect(page.getByText("История неполна")).toBeVisible();

  const intake = page.locator('[data-analytics-metric="identities.added"]');
  await expect(
    intake.getByText(LONG_GROUPS[9] ?? "missing", { exact: true }).first(),
  ).toBeVisible();
  await intake.getByText("Таблица данных", { exact: true }).click();
  await expect(
    intake.getByRole("columnheader", { name: LONG_GROUPS[0], exact: true }),
  ).toBeVisible();

  const csv = page.waitForEvent("download");
  await intake.getByRole("button", { name: "CSV" }).click();
  expect((await csv).suggestedFilename()).toBe("identities-added.csv");
  const svg = page.waitForEvent("download");
  await intake.getByRole("button", { name: "SVG" }).click();
  expect((await svg).suggestedFilename()).toBe("identities-added.svg");
  await expect(page.locator("body")).not.toContainText(
    /ciphertext|lease_token|password/,
  );
});

test("long analytics legend remains readable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openAnalytics(page);
  const intake = page.locator('[data-analytics-metric="identities.added"]');
  await expect(intake).toHaveScreenshot("analytics-long-legend.png", {
    animations: "disabled",
    maxDiffPixelRatio: 0.03,
  });
});
