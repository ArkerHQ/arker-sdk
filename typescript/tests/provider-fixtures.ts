import assert from "node:assert/strict";
import { once } from "node:events";
import type { Server } from "node:http";
import type { TestContext } from "node:test";

export function preserveProperties<T, K extends keyof T>(
  t: TestContext,
  target: T,
  ...keys: K[]
): void {
  const originals = keys.map((key) => [key, target[key]] as const);
  t.after(() => {
    for (const [key, value] of originals) target[key] = value;
  });
}

export function preserveEnvironment(t: TestContext, ...keys: string[]): void {
  const originals = keys.map((key) => [key, process.env[key]] as const);
  t.after(() => {
    for (const [key, value] of originals) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
}

export async function listen(server: Server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address !== "string");
  return address;
}

export async function closeServer(server: Server): Promise<void> {
  server.closeAllConnections();
  await new Promise<void>((resolve, reject) =>
    server.close((error) => (error ? reject(error) : resolve())),
  );
}
