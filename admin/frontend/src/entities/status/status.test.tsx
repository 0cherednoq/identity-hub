import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DomainStatusBadge, LIFECYCLE_VIEW, resolveStatus } from "./status";

describe("domain status registry", () => {
  it("returns the registered view for a known value", () => {
    expect(resolveStatus(LIFECYCLE_VIEW, "ready")).toEqual({
      label: "Готов",
      tone: "success",
      unknown: false,
    });
  });

  it("renders an unknown backend value as a visible warning", () => {
    render(
      <DomainStatusBadge registry={LIFECYCLE_VIEW} value="backend_new_state" />,
    );
    expect(screen.getByText("Неизвестно: backend_new_state")).toBeVisible();
    expect(screen.getByText("Неизвестно: backend_new_state")).toHaveClass(
      "bg-destructive",
    );
  });
});
