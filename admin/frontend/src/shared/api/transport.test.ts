import { describe, expect, it, vi } from "vitest";

import {
  AdminApiError,
  RequestAbortedError,
  createAdminClient,
  requestOptions,
  unwrapApiResult,
} from "./transport";

describe("admin API transport", () => {
  it("sets base path, credentials, request ID, CSRF, and AbortSignal", async () => {
    document.cookie = "ih_admin_csrf=csrf-test; path=/";
    const controller = new AbortController();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (!(input instanceof Request)) {
        throw new TypeError("fetch did not receive a Request");
      }
      return Promise.resolve(
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    const client = createAdminClient({
      basePath: "https://admin.test/root",
      fetch: fetchMock,
    });

    await client.post({
      url: "/api/v1/example",
      ...requestOptions(controller.signal),
    });

    const request = fetchMock.mock.calls[0]?.[0];
    if (!(request instanceof Request)) {
      throw new TypeError("fetch did not receive a Request");
    }
    expect(request.url).toBe("https://admin.test/root/api/v1/example");
    expect(request.credentials).toBe("include");
    expect(request.headers.get("X-Request-ID")).toMatch(/^[0-9a-f-]{36}$/u);
    expect(request.headers.get("X-CSRF-Token")).toBe("csrf-test");
    expect(request.signal.aborted).toBe(false);
    controller.abort();
    expect(request.signal.aborted).toBe(true);
  });

  it("maps error envelopes and marks aborts separately", () => {
    const response = new Response(null, { status: 403 });
    const request = new Request("https://admin.test/api/v1/example");
    const envelope = {
      error: {
        code: "access_denied" as const,
        message: "Access denied",
        details: {},
        request_id: "request-001",
      },
    };

    expect(() =>
      unwrapApiResult({ error: envelope, request, response }),
    ).toThrow(AdminApiError);
    const controller = new AbortController();
    controller.abort();
    expect(() =>
      unwrapApiResult(
        { error: new DOMException("Aborted", "AbortError"), request, response },
        controller.signal,
      ),
    ).toThrow(RequestAbortedError);
  });
});
