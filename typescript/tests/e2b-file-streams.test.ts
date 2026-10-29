import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { serveDataPlane } from "./e2b-http-fixtures.js";

for (const bytes of [Buffer.alloc(0), Buffer.from([0, 128, 255]), Buffer.alloc(6 * 1024 * 1024, 0xab)]) {
  test(`native file stream preserves ${bytes.length} binary bytes`, async t => {
    const data = await serveDataPlane(t, createServer((_req, res) => { res.end(bytes); }));
    const stream = await data.files.read("/home/user/file", { format: "stream" });
    assert.deepEqual(Buffer.from(await new Response(stream).arrayBuffer()), bytes);
  });
}

test("native file download delivers before EOF and cancellation closes the response", async t => {
  let disconnected!: () => void;
  const closed = new Promise<void>(resolve => { disconnected = resolve; });
  const data = await serveDataPlane(t, createServer((_req, res) => { res.on("close", disconnected); res.write(Buffer.from([0, 255])); }));
  const stream = await data.files.read("/home/user/file", { format: "stream" });
  const reader = stream.getReader();
  assert.deepEqual((await reader.read()).value, Uint8Array.of(0, 255));
  await reader.cancel();
  await Promise.race([closed, new Promise((_, reject) => setTimeout(() => reject(new Error("response not cancelled")), 2000))]);
});

test("native file download rejects truncated content", async t => {
  const data = await serveDataPlane(t, createServer((_req, res) => { res.writeHead(200, { "content-length": 100 }); res.write("partial"); setImmediate(() => res.destroy()); }));
  await assert.rejects(async () => {
    const stream = await data.files.read("/home/user/file", { format: "stream" });
    await new Response(stream).arrayBuffer();
  });
});
