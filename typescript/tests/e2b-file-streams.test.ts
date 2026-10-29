import assert from "node:assert/strict";
import { createServer, type RequestListener } from "node:http";
import test from "node:test";
import { createE2BDataPlane } from "../src/compat/e2b-data-plane.js";

async function fixture(handler: RequestListener) {
  const server = createServer(handler);
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address(); assert.ok(address && typeof address !== "string");
  const data = await createE2BDataPlane({ endpoint: `http://127.0.0.1:${address.port}`, envd_version: "0.10.2", envd_access_token: "test-token" }, "test-key");
  return { data, async close() { data.close(); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); } };
}

for (const bytes of [Buffer.alloc(0), Buffer.from([0, 128, 255]), Buffer.alloc(6 * 1024 * 1024, 0xab)]) {
  test(`native file stream preserves ${bytes.length} binary bytes`, async () => {
    const f = await fixture((_req, res) => { res.end(bytes); });
    try {
      const stream = await f.data.files.read("/home/user/file", { format: "stream" });
      assert.deepEqual(Buffer.from(await new Response(stream).arrayBuffer()), bytes);
    } finally { await f.close(); }
  });
}

test("native file download delivers before EOF and cancellation closes the response", async () => {
  let disconnected!: () => void;
  const closed = new Promise<void>(resolve => { disconnected = resolve; });
  const f = await fixture((_req, res) => { res.on("close", disconnected); res.write(Buffer.from([0, 255])); });
  try {
    const stream = await f.data.files.read("/home/user/file", { format: "stream" });
    const reader = stream.getReader();
    assert.deepEqual((await reader.read()).value, Uint8Array.of(0, 255));
    await reader.cancel();
    await Promise.race([closed, new Promise((_, reject) => setTimeout(() => reject(new Error("response not cancelled")), 2000))]);
  } finally { await f.close(); }
});

test("native file download rejects truncated content", async () => {
  const f = await fixture((_req, res) => { res.writeHead(200, { "content-length": 100 }); res.write("partial"); setImmediate(() => res.destroy()); });
  try {
    await assert.rejects(async () => {
      const stream = await f.data.files.read("/home/user/file", { format: "stream" });
      await new Response(stream).arrayBuffer();
    });
  } finally { await f.close(); }
});
