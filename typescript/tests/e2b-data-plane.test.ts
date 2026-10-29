import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import test from "node:test";
import { CommandExitError, SandboxNotRunningError } from "e2b";
import { createE2BDataPlane, scopedEnvdFetch, type E2BDataPlane } from "../src/compat/e2b-data-plane.js";
async function listen(server: Server): Promise<string> {
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address(); assert.ok(address && typeof address !== "string");
  return `http://127.0.0.1:${address.port}`;
}
async function close(server: Server) { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
const operations: Record<string, (data: E2BDataPlane) => Promise<unknown>> = {
  read: data => data.files.read("/tmp/file"), write: data => data.files.write("/tmp/file", "content"),
  stat: data => data.files.getInfo("/tmp/file"), command: data => data.commands.run("true"),
  pty: data => data.pty.create({ rows: 24, cols: 80, onData() {} }), input: data => data.commands.streamInput(123).close(),
};
for (const status of [409, 302, 307, 308, 404]) {
  test(`native scoped transport status ${status}`, async () => {
    let leaks = 0, calls = 0;
    const target = createServer((_req, res) => { leaks++; res.end(); });
    const targetUrl = await listen(target);
    const origin = createServer((req, res) => {
      calls++; assert.equal(req.headers["x-api-key"], "platform-test-key");
      assert.equal(req.headers["x-access-token"], "guest-test-token"); req.resume();
      res.writeHead(status, { "content-type": "application/json", location: targetUrl });
      res.end(JSON.stringify({ error: { code: "invalid_state", details: {
        resource: { kind: "vm", id: "vm" }, requirement: "explicit_resume",
      } } }));
    });
    const endpoint = await listen(origin);
    let data: E2BDataPlane | undefined;
    try {
      data = await createE2BDataPlane({ endpoint, envd_version: "0.10.2", envd_access_token: "guest-test-token" }, "platform-test-key");
      for (const [name, operation] of Object.entries(operations)) {
        await assert.rejects(operation(data), error => status === 409 ? error instanceof SandboxNotRunningError : !(error instanceof SandboxNotRunningError), name);
      }
      assert.equal(calls, Object.keys(operations).length); assert.equal(leaks, 0);
    } finally { data?.close(); await close(origin); await close(target); }
  });
}
test("fetch origin guard, explicit user header, and cancellation", async () => {
  let calls = 0; const controller = new AbortController();
  const scoped = scopedEnvdFetch({ endpoint: "https://envd.test", envd_version: "0.10.2", envd_access_token: "guest" }, "platform", async input => {
    calls++; assert.ok(input instanceof Request); assert.equal(input.redirect, "error");
    assert.equal(input.headers.get("Authorization"), "Basic dXNlcjo=");
    controller.abort(); assert.equal(input.signal.aborted, true); return new Response("ok");
  });
  await assert.rejects(scoped("https://foreign.test/files"), /outside/); assert.equal(calls, 0);
  const response = await scoped("https://envd.test/files", { signal: controller.signal, headers: { Authorization: "Basic dXNlcjo=" } });
  await response.text(); assert.equal(calls, 1);
});

test("RPC cancellation reaches the socket without health probing", async () => {
  let calls = 0;
  let started!: () => void;
  let disconnected!: () => void;
  const start = new Promise<void>(resolve => { started = resolve; });
  const end = new Promise<void>(resolve => { disconnected = resolve; });
  const server = createServer((req, res) => {
    calls++; req.resume(); res.on("close", disconnected); started();
  });
  const endpoint = await listen(server);
  let data: E2BDataPlane | undefined;
  try {
    data = await createE2BDataPlane({ endpoint, envd_version: "0.10.2", envd_access_token: "guest" }, "platform");
    const controller = new AbortController();
    const pending = data.commands.run("sleep 60", { signal: controller.signal });
    const rejection = assert.rejects(pending);
    await start; controller.abort(); await rejection;
    await Promise.race([end, new Promise((_, reject) => setTimeout(() => reject(new Error("socket not cancelled")), 1000))]);
    assert.equal(calls, 1);
  } finally { data?.close(); await close(server); }
});

test("409 reads are bounded, cancel the source, and preserve unrelated conflict status", async () => {
  let cancelled = false;
  const scoped = scopedEnvdFetch({ endpoint: "https://envd.test", envd_version: "0.10.2", envd_access_token: "guest" }, "platform", async () =>
    new Response(new ReadableStream<Uint8Array>({
      pull(controller) { controller.enqueue(new Uint8Array(32 * 1024).fill(120)); },
      cancel() { cancelled = true; },
    }), { status: 409 }));
  const result = await scoped("https://envd.test/files");
  assert.equal(result.status, 409);
  assert.equal((await result.arrayBuffer()).byteLength, 64 * 1024);
  assert.equal(cancelled, true);
});

test("close aborts owned RPC requests and refuses reuse without network I/O", async () => {
  let started!: () => void;
  const pendingRequest = new Promise<void>(resolve => { started = resolve; });
  let calls = 0;
  const server = createServer((req, _res) => { calls++; req.resume(); started(); });
  const endpoint = await listen(server);
  let data: E2BDataPlane | undefined;
  try {
    data = await createE2BDataPlane({ endpoint, envd_version: "0.10.2", envd_access_token: "guest" }, "platform");
    const failure = assert.rejects(data.commands.run("sleep 60"));
    await pendingRequest; data.close(); data.close(); await failure;
    await assert.rejects(data.files.read("/tmp/file"), /closed/);
    assert.equal(calls, 1);
  } finally { data?.close(); await close(server); }
});

test("native command exit retains the plain e2b consumer's error class", async () => {
  const frame = (value: unknown, flags = 0) => {
    const payload = Buffer.from(JSON.stringify(value)), header = Buffer.alloc(5);
    header[0] = flags; header.writeUInt32BE(payload.length, 1);
    return Buffer.concat([header, payload]);
  };
  const server = createServer((req, res) => {
    req.resume(); res.writeHead(200, { "content-type": "application/connect+json" });
    res.write(frame({ event: { start: { pid: 123 } } }));
    res.write(frame({ event: { end: { exitCode: 7, exited: true, status: "exited" } } }));
    res.end(frame({}, 2));
  });
  const endpoint = await listen(server);
  let data: E2BDataPlane | undefined;
  try {
    data = await createE2BDataPlane({ endpoint, envd_version: "0.10.2", envd_access_token: "guest" }, "platform");
    await assert.rejects(data.commands.run("exit 7"), error => error instanceof CommandExitError && error.exitCode === 7);
  } finally { data?.close(); await close(server); }
});

test("RPC header failures release request ownership before the data plane closes", async () => {
  const aborts = new Map<AbortController, number>();
  const originalAbort = AbortController.prototype.abort;
  AbortController.prototype.abort = function (reason?: unknown) {
    aborts.set(this, (aborts.get(this) ?? 0) + 1);
    originalAbort.call(this, reason);
  };
  const server = createServer((req, res) => {
    req.resume();
    res.writeHead(401, { "content-type": "application/json" });
    res.end("{}");
  });
  let data: E2BDataPlane | undefined;
  try {
    const endpoint = await listen(server);
    data = await createE2BDataPlane({ endpoint, envd_version: "0.10.2", envd_access_token: "guest" }, "platform");
    for (let i = 0; i < 5; i++) await assert.rejects(data.commands.run("true"));
    // A completed call must not remain owned and be aborted again by close().
    const completed = new Map(aborts);
    assert.ok(completed.size > 0);
    data.close();
    for (const [controller, count] of completed) assert.equal(aborts.get(controller), count);
  } finally {
    data?.close();
    AbortController.prototype.abort = originalAbort;
    await close(server);
  }
});

test("already-aborted REST signals reach the transport", async () => {
  const controller = new AbortController();
  controller.abort();
  let signal: AbortSignal | undefined;
  const scoped = scopedEnvdFetch({ endpoint: "https://envd.test", envd_version: "0.10.2", envd_access_token: "guest" },
    "platform", async input => {
      assert.ok(input instanceof Request);
      signal = input.signal;
      return new Response(null, { status: 204 });
    });
  await scoped("https://envd.test/files", { signal: controller.signal });
  assert.equal(signal?.aborted, true);
});
