import { describe, expect, it } from "vitest";

import { ADMIN_ERROR_LABELS } from "./error-codes";

describe("generated error codes", () => {
  it("have an exhaustive localized label registry", () => {
    expect(Object.keys(ADMIN_ERROR_LABELS)).toHaveLength(10);
  });
});
