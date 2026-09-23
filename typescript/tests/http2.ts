// Exercises the real node:http2 transport (skipped by every other suite, which injects
// a custom fetch and so disables HTTP/2). Drives the SDK against a local h2 server to
// cover the happy path, connection multiplexing, and that a server-aborted stream
// settles the caller instead of hanging.

import assert from "node:assert/strict";
import http2 from "node:http2";
import http from "node:http";
import { type AddressInfo } from "node:net";

import { Arker, type CompletedRunResult } from "../src/index.js";

// A completed-run body shaped like the real API response.
const RUN_BODY = JSON.stringify({
  stdout: "hi\n",
  stdout_encoding: "utf-8",
  stderr: "",
  stderr_encoding: "utf-8",
  exit_code: 0,
});

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function listen(server: http2.Http2Server): Promise<number> {
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve((server.address() as AddressInfo).port));
  });
}

// An Arker pointed at a local server with HTTP/2 ENABLED — i.e. no custom fetch, so
// `this.http2` is true and requests go through the node:http2 path under test.
function h2client(port: number): Arker {
  return new Arker({
    apiKey: "ark_live_test",
    baseUrl: `http://127.0.0.1:${port}/api`,
    retry: false,
  });
}

// Track server-side sessions so cleanup can tear them down; otherwise the kept-alive
// client connection keeps server.close() (and the process) from ever finishing.
function trackSessions(server: http2.Http2Server): Set<http2.ServerHttp2Session> {
  const sessions = new Set<http2.ServerHttp2Session>();
  server.on("session", (session) => {
    sessions.add(session);
    session.on("close", () => sessions.delete(session));
  });
  return sessions;
}

async function shutdown(server: http2.Http2Server, sessions: Set<http2.ServerHttp2Session>): Promise<void> {
  for (const session of sessions) session.destroy();
  await new Promise<void>((resolve) => server.close(() => resolve()));
}

async function testHttp2HappyPath(): Promise<void> {
  const received: http2.IncomingHttpHeaders[] = [];
  const server = http2.createServer();
  const sessions = trackSessions(server);
  server.on("stream", (stream: http2.ServerHttp2Stream, headers: http2.IncomingHttpHeaders) => {
    received.push(headers);
    stream.respond({ ":status": 200, "content-type": "application/json" });
    stream.end(RUN_BODY);
  });
  const port = await listen(server);

  const result = await h2client(port).vm("vm_1").run("printf hi", { idempotencyKey: "key-abc" });

  assert.equal((result as CompletedRunResult).exitCode, 0);
  const headers = received[0]!;
  assert.equal(headers[":method"], "POST");
  assert.ok(String(headers[":path"]).endsWith("/v1/vms/vm_1/runs"));
  assert.equal(headers["authorization"], "Bearer ark_live_test");
  // The idempotency key set by run() round-trips over HTTP/2 (field names are
  // lowercase on the wire, so it arrives as "idempotency-key").
  assert.equal(headers["idempotency-key"], "key-abc");

  await shutdown(server, sessions);
}

async function testHttp2MultiplexesConcurrentRequests(): Promise<void> {
  const server = http2.createServer();
  const sessions = trackSessions(server);
  server.on("stream", (stream: http2.ServerHttp2Stream) => {
    stream.respond({ ":status": 200, "content-type": "application/json" });
    stream.end(RUN_BODY);
  });
  const port = await listen(server);

  const arker = h2client(port);
  const warnings: Error[] = [];
  const onWarning = (warning: Error) => {
    if (warning.name === "MaxListenersExceededWarning") warnings.push(warning);
  };
  process.on("warning", onWarning);
  await Promise.all(Array.from({ length: 5 }, () => arker.vm("vm_1").run("printf hi")));
  for (let i = 0; i < 20; i++) await arker.vm("vm_1").run("printf hi");
  await sleep(0);

  process.off("warning", onWarning);
  assert.deepEqual(warnings, [], "reused requests must not leak timeout listeners");
  assert.equal(sessions.size, 1, `expected one multiplexed session, got ${sessions.size}`);

  await shutdown(server, sessions);
}

async function testAbortedRequestSettlesWithoutHanging(): Promise<void> {
  let streamCount = 0;
  const server = http2.createServer();
  const sessions = trackSessions(server);
  server.on("stream", (stream: http2.ServerHttp2Stream) => {
    streamCount++;
    if (streamCount === 1) {
      // First request succeeds, so the client marks this origin as HTTP/2.
      stream.respond({ ":status": 200, "content-type": "application/json" });
      stream.end(RUN_BODY);
      return;
    }
    // Second request: abort it mid-flight with no response (RST_STREAM).
    stream.close();
  });
  const port = await listen(server);

  const arker = h2client(port);
  await arker.vm("vm_1").run("printf hi"); // confirm h2 on this origin first

  // The aborted request must SETTLE (reject), not hang.
  const outcome = await Promise.race([
    arker.vm("vm_1").run("printf hi").then(() => "resolved", () => "rejected"),
    sleep(3000).then(() => "HUNG"),
  ]);
  assert.equal(outcome, "rejected", "an aborted HTTP/2 stream must reject, not hang");

  await shutdown(server, sessions);
}

async function testHttp1MutationWorksWithoutAnEarlierRead(): Promise<void> {
  const requests: string[] = [];
  const server = http.createServer((req, res) => {
    requests.push(`${req.method} ${req.url}`);
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ deleted: true }));
  });
  const port = await new Promise<number>((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve((server.address() as AddressInfo).port));
  });
  try {
    assert.deepEqual(await h2client(port).vm("vm_1").delete(), { deleted: true });
    assert.deepEqual(requests, ["DELETE /api/v1/vms/vm_1"]);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

async function testFirstHttp2MutationFailureDoesNotReplayOrDisableHttp2(): Promise<void> {
  let requests = 0;
  let connections = 0;
  const server = http2.createServer();
  const sessions = trackSessions(server);
  server.on("connection", () => connections++);
  server.on("stream", (stream: http2.ServerHttp2Stream) => {
    requests++;
    if (requests === 1) {
      stream.close();
      return;
    }
    stream.respond({ ":status": 200, "content-type": "application/json" });
    stream.end(RUN_BODY);
  });
  const port = await listen(server);
  const arker = h2client(port);
  try {
    await assert.rejects(
      () => arker.vm("vm_1").run("printf once", { idempotencyKey: "one-attempt" }),
      (error: unknown) => error instanceof Error,
    );
    await sleep(20);
    assert.equal(requests, 1, "a dispatched HTTP/2 mutation must not be retried");
    assert.equal(connections, 1, "a dispatched HTTP/2 mutation must not fall back to fetch");
    const result = await arker.vm("vm_1").run("printf after-reconcile");
    assert.equal((result as CompletedRunResult).exitCode, 0);
    assert.equal(requests, 2, "an ambiguous failure must not disable HTTP/2 for later requests");
  } finally {
    await shutdown(server, sessions);
  }
}

await testHttp2HappyPath();
await testHttp2MultiplexesConcurrentRequests();
await testAbortedRequestSettlesWithoutHanging();
await testFirstHttp2MutationFailureDoesNotReplayOrDisableHttp2();
await testHttp1MutationWorksWithoutAnEarlierRead();

console.log("PASS http2");
// Real sockets (server sessions) can keep the event loop alive; everything is
// asserted, so exit deterministically.
process.exit(0);
