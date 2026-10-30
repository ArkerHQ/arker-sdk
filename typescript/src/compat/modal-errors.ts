import { createRequire } from "node:module";
import { ConflictError } from "modal";
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
  let mapped: Error;
  if (deleted) {
    const require = createRequire(typeof import.meta.url === "string" ? import.meta.url : __filename);
    const { ClientError, Status } = createRequire(require.resolve("modal"))("nice-grpc");
    mapped = new ClientError(
      "/modal.task_command_router.TaskCommandRouter/TaskExecStart",
      Status.NOT_FOUND,
      "Sandbox is no longer available",
    );
  } else {
    mapped = new ConflictError("Sandbox is shutting down");
  }
  mapped.cause = error;
  throw mapped;
}
