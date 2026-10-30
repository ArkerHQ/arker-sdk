import { DaytonaConnectionTimeoutError, DaytonaError, DaytonaInvalidArgumentError } from "@daytona/sdk";
import { withRequestTransport } from "../internal/request-context.js";
import { RequestIdleTimeoutError } from "../internal/idle-fetch.js";

export function validateDaytonaRequestTimeout(value: number | undefined): void {
  if (value !== undefined && (!Number.isFinite(value) || value < 0)) {
    throw new DaytonaInvalidArgumentError("requestTimeoutMs must be a non-negative finite number of milliseconds");
  }
}

export async function daytonaRequest<T>(milliseconds: number | undefined, operation: () => Promise<T>): Promise<T> {
  try { return await withRequestTransport({idleTimeoutMs: milliseconds ?? 86_400_000}, operation); }
  catch (error) {
    if (error instanceof RequestIdleTimeoutError) {
      const mapped = error.responseStarted ? new DaytonaError("stream has been aborted") : new DaytonaConnectionTimeoutError(error.message);
      for (const field of ["sessionId", "processId"] as const) {
        if (field in error) Object.defineProperty(mapped, field, {value: (error as unknown as Record<string, unknown>)[field]});
      }
      throw mapped;
    }
    throw error;
  }
}

export function executionRequestTimeout(configured: number | undefined, seconds: number | undefined): number | undefined {
  if (seconds === undefined || configured === undefined || configured <= 0) return configured;
  return seconds > 0 ? seconds * 1000 + 5000 : 0;
}
