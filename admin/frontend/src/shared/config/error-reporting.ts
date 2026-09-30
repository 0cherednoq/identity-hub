import { useEffect } from "react";

export interface FrontendErrorEvent {
  error: unknown;
  requestId?: string;
  source: string;
}

export type ErrorReporter = (event: FrontendErrorEvent) => void | Promise<void>;

let reporter: ErrorReporter | null = null;

export function configureErrorReporter(next: ErrorReporter | null): void {
  reporter = next;
}

export function reportFrontendError(event: FrontendErrorEvent): void {
  if (reporter === null) return;
  try {
    void Promise.resolve(reporter(event)).catch(() => undefined);
  } catch {
    // Reporting must never replace the original UI error.
  }
}

export function useErrorReporting(event: FrontendErrorEvent | null): void {
  useEffect(() => {
    if (event !== null) reportFrontendError(event);
  }, [event]);
}
