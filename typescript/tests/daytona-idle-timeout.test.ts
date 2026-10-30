import assert from "node:assert/strict";
import { createServer, type RequestListener } from "node:http";
import { test } from "node:test";
import { idleFetch, RequestIdleTimeoutError } from "../src/internal/idle-fetch.js";

async function server(handler: RequestListener, run: (url: string) => Promise<void>) {
  const http = createServer(handler);
  await new Promise<void>(resolve => http.listen(0, "127.0.0.1", resolve));
  const address = http.address() as { port: number };
  try { await run(`http://127.0.0.1:${address.port}`); }
  finally { http.closeAllConnections(); await new Promise<void>(resolve => http.close(() => resolve())); }
}

test("idle timeout permits a drip response longer than its timeout", async () => {
  await server((_req, res) => {
    res.writeHead(200); res.write("a");
    const timer = setInterval(() => res.write("b"), 20);
    const end = setTimeout(() => { clearInterval(timer); res.end("c"); }, 180);
    res.on("close", () => { clearInterval(timer); clearTimeout(end); });
  }, async url => {
    const started = Date.now();
    const response = await idleFetch(fetch, url, {}, 100);
    assert.match(await response.text(), /^ab+c$/);
    assert.ok(Date.now() - started >= 180);
  });
});

for (const headers of [false, true]) test(`idle timeout closes stalled response headers=${headers}`, async () => {
  let closed!: () => void;
  const closure = new Promise<void>(resolve => { closed = resolve; });
  await server((_req, res) => { if (headers) { res.writeHead(200); res.write("a"); } res.on("close", closed); }, async url => {
    await assert.rejects(async () => (await idleFetch(fetch, url, {}, 50)).text(), RequestIdleTimeoutError);
    await closure;
  });
});

test("zero disables timeout and cancellation preserves caller identity", async () => {
  await server((_req, res) => { const timer = setTimeout(() => res.end("ok"), 60); res.on("close", () => clearTimeout(timer)); }, async url => {
    assert.equal(await (await idleFetch(fetch, url, {}, 0)).text(), "ok");
    const abort = new AbortController(); const reason = new Error("caller cancellation");
    const response = idleFetch(fetch, url, { signal: abort.signal }, 1000);
    abort.abort(reason);
    await assert.rejects(response, error => error === reason);
  });
});

test("run transport timeout does not serialize a guest deadline or retry mutation", async () => {
  const { Arker } = await import("../src/index.js");
  const bodies: unknown[] = [];
  await server((req, res) => {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", () => { bodies.push(JSON.parse(body)); });
  }, async url => {
    const client = new Arker({apiKey: "synthetic", baseUrl: url});
    const vm = client.vm("vm-test");
    await assert.rejects(vm._runWithTransport("sleep 100", {}, {idleTimeoutMs: 50}), RequestIdleTimeoutError);
    assert.deepEqual(bodies, [{command: "sleep 100"}]);
  });
});

test("presigned stream retains timeout after headers and cancels on early return", async () => {
  const { Arker, VM } = await import("../src/index.js");
  let stopped!: () => void;
  const closed = new Promise<void>(resolve => { stopped = resolve; });
  await server((_req, res) => {
    res.writeHead(200); res.write("first");
    res.on("close", stopped);
  }, async url => {
    const client = new Arker({apiKey: "synthetic", baseUrl: url});
    const vm = new VM({
      _request: async (...args: unknown[]) => {
        assert.equal((args[7] as {idleTimeoutMs: number}).idleTimeoutMs, 50);
        return {presigned_url: url};
      },
      _download: client._download.bind(client),
    } as any, "vm-test", url);
    const chunks = vm._syncReadChunks("/file", undefined, 50);
    assert.equal(Buffer.from((await chunks.next()).value!).toString(), "first");
    await assert.rejects(chunks.next(), RequestIdleTimeoutError);
    await closed;
  });
});
