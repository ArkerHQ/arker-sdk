import type { Server } from "node:http";
import type { TestContext } from "node:test";
import { createE2BDataPlane, type E2BDataPlane } from "../src/compat/e2b-data-plane.js";
import { listen, closeServer } from "./provider-fixtures.js";

export async function serveDataPlane(t: TestContext, server: Server, requestTimeoutMs?: number) {
  let data: E2BDataPlane | undefined;
  t.after(async () => {
    try { data?.close(); } finally { await closeServer(server); }
  });
  const address = await listen(server);
  data = await createE2BDataPlane({ endpoint: `http://127.0.0.1:${address.port}`,
    envd_version: "0.10.2", envd_access_token: "guest" }, "platform", requestTimeoutMs);
  return data;
}
