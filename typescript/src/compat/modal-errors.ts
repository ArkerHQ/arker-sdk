import { ConflictError, NotFoundError } from "modal";
import type { ArkerError } from "../index.js";

import { validateHttpError } from "../generated/error-validators.js";

export function missingVm(error: unknown): error is ArkerError {
  // Separate package entrypoints can contain different ArkerError constructors.
  if (!(error instanceof Error) || error.name !== "ArkerError") return false;
  const candidate = error as ArkerError;
  return candidate.status === 404 && candidate.code === "not_found"
    && validateHttpError(candidate.body) && candidate.body.code === "not_found"
    && candidate.body.details.resource === "vm";
}

export async function rethrowModalExecError(error: unknown, refresh: () => Promise<unknown>): Promise<never> {
  const primary = error instanceof AggregateError ? error.errors[0] : error;
  if (!missingVm(primary)) throw error;
  let deleted = false;
  try { await refresh(); }
  catch (observed) {
    if (!missingVm(observed)) throw error;
    deleted = true;
  }
  const mapped = deleted
    ? new NotFoundError("Sandbox is no longer available")
    : new ConflictError("Sandbox is shutting down");
  mapped.cause = error;
  throw mapped;
}
