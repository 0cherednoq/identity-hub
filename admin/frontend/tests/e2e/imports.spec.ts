import { expect, test } from "@playwright/test";

import { authenticateAs } from "./support/auth";

const RUN_ID = "00000000-0000-0000-0000-000000009701";
const ACCOUNT_ID = "00000000-0000-0000-0000-000000009702";

function importRun(state: "pending" | "succeeded") {
  const complete = state === "succeeded";
  return {
    id: RUN_ID,
    service: "demo_account",
    actor: "e2e:operator",
    source: "mixed.jsonl",
    state,
    on_existing: "skip",
    verify_sessions: false,
    labels: { batch: "e2e" },
    started_at: "2026-09-27T12:00:00Z",
    updated_at: "2026-09-27T12:00:01Z",
    finished_at: complete ? "2026-09-27T12:00:01Z" : null,
    cancel_requested_at: null,
    total: 3,
    processed: complete ? 3 : 0,
    created: complete ? 1 : 0,
    updated: 0,
    skipped: complete ? 1 : 0,
    failed: complete ? 1 : 0,
    created_ids: complete ? [ACCOUNT_ID] : [],
    skipped_items: complete
      ? [
          {
            position: 2,
            external_ref: "existing-account",
            code: "already_exists",
            reason: "already exists",
          },
        ]
      : [],
    failed_items: complete
      ? [
          {
            position: 3,
            external_ref: "rejected-account",
            code: "record_rejected",
            reason: "profile rejected",
          },
        ]
      : [],
  };
}

test("operator uploads a mixed JSONL batch and opens its safe report", async ({
  page,
}) => {
  await authenticateAs(page, "operator");
  let completed = false;
  let submitted: Record<string, unknown> | null = null;
  let submittedContent = "";
  await page.route("**/api/v1/services", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          items: [{ name: "demo_account", title: "Demo accounts" }],
        },
      }),
    });
  });
  await page.route("**/api/v1/spaces/demo-large/imports", async (route) => {
    if (route.request().method() === "POST") {
      submitted = route.request().postDataJSON() as Record<string, unknown>;
      submittedContent =
        typeof submitted.content === "string" ? submitted.content : "";
      completed = true;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({ data: importRun("pending") }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          items: completed ? [importRun("succeeded")] : [],
          limits: {
            max_bytes: 10_485_760,
            max_records: 10_000,
            max_parse_seconds: 5,
          },
        },
      }),
    });
  });

  await page.goto("/spaces/demo-large/imports");
  await page.getByLabel("Перетащите файл или выберите его").setInputFiles({
    name: "mixed.jsonl",
    mimeType: "application/x-ndjson",
    buffer: Buffer.from(
      [
        '{"external_ref":"new-account"}',
        '{"external_ref":"existing-account"}',
        '{"external_ref":"rejected-account"}',
      ].join("\n"),
    ),
  });
  await page.getByLabel("Сервис импорта").click();
  await page.getByRole("option", { name: "Demo accounts" }).click();
  await page.getByLabel("Причина").fill("e2e approved migration");
  await page.getByLabel("Метки партии").fill("batch=e2e");
  await page.getByRole("button", { name: "Запустить импорт" }).click();

  await expect(page.getByText("Завершён")).toBeVisible();
  await expect(
    page.getByText(/1 создано · 0 обновлено · 1 пропущено · 1 ошибок/u),
  ).toBeVisible();
  await expect(page.getByText(/Строка 2: existing-account/u)).toBeVisible();
  await expect(page.getByText(/Строка 3: rejected-account/u)).toBeVisible();
  await expect(
    page.getByRole("link", { name: /Открыть 00000000/u }),
  ).toBeVisible();
  expect(submitted).toMatchObject({
    source: "mixed.jsonl",
    format: "jsonl",
    service: "demo_account",
    labels: { batch: "e2e" },
  });
  expect(submittedContent).toContain("existing-account");
});
