import { ArkerError } from "../index.js";

export function blaxelNotFound() {
  return { code: 404, error: "Sandbox not found" };
}

export function blaxelControlError(error: unknown, operation: "create" | "get" | "delete"): unknown {
  if (!(error instanceof ArkerError)) return error;
  const prefix = `${error.code}: `;
  const message = error.body?.message ?? (error.message.startsWith(prefix) ? error.message.slice(prefix.length) : error.message);
  let translated: object;
  if (operation !== "create" && error.status === 404 && error.code === "not_found") {
    translated = blaxelNotFound();
  } else if (error.status === 401 || error.status === 403) {
    translated = operation === "create"
      ? { code: error.code, message, status_code: error.status }
      : { code: error.status, error: message };
  } else {
    // A transport failure or timeout does not prove creation was cancelled.
    return error;
  }
  return Object.defineProperty(translated, "cause", { value: error });
}

export async function blaxelControlCall<T>(operation: "create" | "get" | "delete", call: () => Promise<T>): Promise<T> {
  try { return await call(); }
  catch (error) { throw blaxelControlError(error, operation); }
}
