import { describe, expect, it, vi } from "vitest";

import { configureErrorReporter, reportFrontendError } from "./error-reporting";

describe("optional frontend error reporter", () => {
  it("reports when configured and remains a no-op without an adapter", () => {
    const adapter = vi.fn();
    configureErrorReporter(adapter);
    reportFrontendError({ error: new Error("boom"), source: "test" });
    expect(adapter).toHaveBeenCalledOnce();
    configureErrorReporter(null);
    expect(() => {
      reportFrontendError({ error: new Error("ignored"), source: "test" });
    }).not.toThrow();
  });

  it("does not throw when an adapter fails synchronously", () => {
    configureErrorReporter(() => {
      throw new Error("reporter unavailable");
    });
    expect(() => {
      reportFrontendError({ error: new Error("original"), source: "test" });
    }).not.toThrow();
    configureErrorReporter(null);
  });
});
