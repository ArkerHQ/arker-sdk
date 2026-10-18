import assert from "node:assert/strict";
import { createServer } from "node:http";
import { connect } from "node:net";
import { once } from "node:events";
import { test } from "node:test";
import { abortAwareBody, privateFetch } from "../src/compat/private-fetch.js";

test("aborted HTTP bodies preserve the abort reason across relay reset races", async () => {
  for (const aborted of [false, true]) {
    const controller = new AbortController();
    let fail!: (reason: Error) => void;
    const source = new ReadableStream<Uint8Array>({
      start(stream) {
        fail = (reason) => stream.error(reason);
      },
    });
    const body = abortAwareBody(source, controller.signal);
    const reader = body.getReader();
    const pending = reader.read();
    await Promise.resolve();
    if (aborted) controller.abort();
    const reset = Object.assign(new Error("aborted"), { code: "ECONNRESET" });
    fail(reset);
    await assert.rejects(pending, aborted ? { name: "AbortError" } : reset);
    await assert.rejects(
      reader.cancel(),
      aborted ? { name: "AbortError" } : reset,
    );
  }
});

test("private HTTP preserves binary bodies, credentials, errors and streaming cleanup", async () => {
  const sockets = new Set<any>();
  const server = createServer(async (request, response) => {
    if (request.url === "/redirect") {
      response.writeHead(302, { location: "chunked" });
      response.end();
      return;
    }
    if (request.url === "/outside") {
      response.writeHead(302, { location: "http://example.com/" });
      response.end();
      return;
    }
    if (request.method === "HEAD") {
      response.writeHead(200, { "x-head": "kept" });
      response.end();
      return;
    }
    if (request.url === "/chunked") {
      response.writeHead(418);
      response.write(Buffer.from("eof"));
      response.end(Buffer.from([0, 255, 1]));
      return;
    }
    if (request.url === "/quiet") {
      response.writeHead(200);
      response.flushHeaders();
      return;
    }
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    response.writeHead(207, {
      "x-authorization": request.headers.authorization!,
      "x-key": request.headers["x-api-key"]!,
    });
    response.end(Buffer.concat(chunks));
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const target = (server.address() as { port: number }).port;
  const opened: number[] = [];
  const streams: any[] = [];
  const vm = {
    connectPort: async (port: number) => {
      opened.push(port);
      const socket = connect(target, "127.0.0.1");
      streams.push(socket);
      await once(socket, "connect");
      return socket;
    },
  };
  try {
    const body = Buffer.from(
      Array.from({ length: 262144 }, (_, index) => index % 256),
    );
    const response = await privateFetch(vm, 8080, "echo?x=1", {
      method: "POST",
      body,
      headers: [
        ["authorization", "Bearer application-only"],
        ["x-api-key", "earlier-value"],
        ["x-api-key", "application-key"],
      ],
    });
    assert.equal(response.status, 207);
    assert.equal(
      response.headers.get("x-authorization"),
      "Bearer application-only",
    );
    assert.equal(response.headers.get("x-key"), "application-key");
    const clone = response.clone();
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), body);
    assert.deepEqual(Buffer.from(await clone.arrayBuffer()), body);
    assert.throws(() => response.headers.set("modified", "yes"), /immutable/);
    const chunked = await privateFetch(vm, 8081, "/redirect");
    assert.equal(chunked.status, 418);
    assert.equal(chunked.redirected, true);
    assert.match(chunked.url, /\/chunked$/);
    assert.deepEqual(
      Buffer.from(await chunked.arrayBuffer()),
      Buffer.from([101, 111, 102, 0, 255, 1]),
    );
    const quiet = await privateFetch(vm, 8082, "/quiet");
    await quiet.body!.cancel();
    assert.equal(streams.at(-1).destroyed, true);
    assert.deepEqual(opened, [8080, 8081, 8081, 8082]);
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(
      () => privateFetch(vm, 8083, "/quiet", { signal: controller.signal }),
      { name: "AbortError" },
    );
    assert.equal(opened.length, 4);
    const manual = await privateFetch(vm, 8080, "/redirect", {
      redirect: "manual",
    });
    assert.equal(manual.status, 302);
    assert.equal(manual.redirected, false);
    await manual.body!.cancel();
    await assert.rejects(
      () => privateFetch(vm, 8080, "/redirect", { redirect: "error" }),
      /Redirect encountered/,
    );
    await assert.rejects(() => privateFetch(vm, 8080, "/outside"), /outside/);
    const head = await privateFetch(vm, 8080, "/head", { method: "HEAD" });
    assert.equal(head.body, null);
    assert.equal(head.headers.get("x-head"), "kept");
    const h2Head = await privateFetch(
      vm,
      8080,
      "/head",
      { method: "HEAD" },
      "blaxel-h2",
    );
    assert.notEqual(h2Head.body, null);
    assert.equal((await h2Head.arrayBuffer()).byteLength, 0);
    assert.equal(h2Head.url, "");
    assert.equal(h2Head.type, "default");
    h2Head.headers.set("mutable", "yes");
    const h2Redirect = await privateFetch(
      vm,
      8080,
      "/redirect",
      undefined,
      "blaxel-h2",
    );
    assert.equal(h2Redirect.status, 302);
    assert.equal(h2Redirect.redirected, false);
    await h2Redirect.body!.cancel();
    const abort = new AbortController();
    // A relay can report socket closure before the request's abort listener runs.
    abort.signal.addEventListener(
      "abort",
      () => {
        streams
          .at(-1)
          .destroy(Object.assign(new Error("aborted"), { code: "ECONNRESET" }));
      },
      { once: true },
    );
    const waiting = await privateFetch(vm, 8080, "/quiet", {
      signal: abort.signal,
    });
    const read = waiting.body!.getReader().read();
    abort.abort();
    await assert.rejects(read, { name: "AbortError" });
    assert.equal(streams.at(-1).destroyed, true);
    for (const port of [0, -1, 65536, NaN])
      await assert.rejects(() => privateFetch(vm, port), RangeError);
  } finally {
    for (const socket of sockets) socket.destroy();
    for (const socket of streams) socket.destroy();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test("abort before private-port upgrade disposes its late result", async () => {
  let resolve!: (stream: any) => void;
  const pending = new Promise<any>((done) => {
    resolve = done;
  });
  const controller = new AbortController();
  const fetch = privateFetch({ connectPort: () => pending }, 8080, "/", {
    signal: controller.signal,
  });
  controller.abort();
  await assert.rejects(fetch, { name: "AbortError" });
  let destroyed = false;
  resolve({
    destroy: () => {
      destroyed = true;
    },
  });
  await new Promise<void>((done) => setImmediate(done));
  assert.equal(destroyed, true);
});
