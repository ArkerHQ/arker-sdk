import { SandboxNotRunningError } from "e2b";
import { ArkerError } from "../index.js";

/** Only the explicit-resume admission refusal identifies this E2B condition. */
export function e2bAdmissionError(error: unknown): unknown {
  if (error instanceof ArkerError && error.status === 409 && error.code === "invalid_state") {
    const body = error.body as { details?: { requirement?: string; resource?: { kind?: string } } } | undefined;
    if (body?.details?.requirement === "explicit_resume" && body.details.resource?.kind === "vm") {
      return new SandboxNotRunningError("Sandbox is paused; call Sandbox.connect() to resume it");
    }
  }
  return error;
}
