import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AdminApiError } from "@/shared/api/transport";

import { AdminErrorState } from "./admin-error-state";

const cases = [
  [401, "Нужен вход"],
  [403, "Недостаточно прав"],
  [404, "Не найдено"],
  [409, "Конфликт"],
  [412, "Версия устарела"],
] as const;

describe("AdminErrorState", () => {
  it.each(cases)(
    "maps HTTP %s to an operator-facing boundary",
    (status, title) => {
      render(
        <AdminErrorState
          error={
            new AdminApiError(status, {
              error: {
                code: status === 412 ? "revision_mismatch" : "conflict",
                message: "Backend message",
                request_id: `request-${String(status)}`,
              },
            })
          }
        />,
      );
      expect(screen.getByText(title)).toBeVisible();
      expect(
        screen.getByText(`Request ID: request-${String(status)}`),
      ).toBeVisible();
    },
  );
});
