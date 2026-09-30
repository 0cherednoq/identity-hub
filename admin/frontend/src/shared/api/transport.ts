import {
  createClient,
  createConfig,
  type Client,
  type Config,
} from "./generated/client";
import type { ErrorEnvelope } from "./generated/types.gen";

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export interface AdminClientOptions {
  basePath?: string;
  fetch?: Config["fetch"];
}

export interface ApiResult<Data> {
  data?: Data | undefined;
  error?: unknown;
  request?: Request;
  response?: Response;
}

export class AdminApiError extends Error {
  readonly body: ErrorEnvelope | null;
  readonly status: number;

  constructor(status: number, body: ErrorEnvelope | null) {
    super(
      body?.error.message ??
        `Admin API request failed with HTTP ${String(status)}`,
    );
    this.name = "AdminApiError";
    this.status = status;
    this.body = body;
  }
}

export class RequestAbortedError extends Error {
  constructor() {
    super("Request was aborted");
    this.name = "RequestAbortedError";
  }
}

function cookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  for (const part of document.cookie.split(";")) {
    const item = part.trim();
    if (item.startsWith(prefix)) {
      return decodeURIComponent(item.slice(prefix.length));
    }
  }
  return null;
}

function requestId(): string {
  return globalThis.crypto.randomUUID();
}

export function createAdminClient(options: AdminClientOptions = {}): Client {
  const config = createConfig({
    baseUrl: options.basePath ?? "/",
    credentials: "include",
    ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
  });
  const client = createClient(config);
  client.interceptors.request.use((request) => {
    const headers = new Headers(request.headers);
    if (!headers.has("X-Request-ID")) {
      headers.set("X-Request-ID", requestId());
    }
    if (MUTATING_METHODS.has(request.method.toUpperCase())) {
      const csrf = cookie("ih_admin_csrf");
      if (csrf !== null && !headers.has("X-CSRF-Token")) {
        headers.set("X-CSRF-Token", csrf);
      }
    }
    return new Request(request, { headers });
  });
  return client;
}

export function requestOptions(signal?: AbortSignal): { signal?: AbortSignal } {
  return signal === undefined ? {} : { signal };
}

export function isRequestAborted(
  error: unknown,
  signal?: AbortSignal,
): boolean {
  return (
    signal?.aborted === true ||
    (error instanceof DOMException && error.name === "AbortError")
  );
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error = value.error;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    "request_id" in error
  );
}

export function unwrapApiResult<Data>(
  result: ApiResult<Data>,
  signal?: AbortSignal,
): Data {
  if (result.data !== undefined) {
    return result.data;
  }
  if (isRequestAborted(result.error, signal)) {
    throw new RequestAbortedError();
  }
  throw new AdminApiError(
    result.response?.status ?? 0,
    isErrorEnvelope(result.error) ? result.error : null,
  );
}
