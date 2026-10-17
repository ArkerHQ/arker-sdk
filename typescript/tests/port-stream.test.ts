import assert from "node:assert/strict";
import { once } from "node:events";
import { test } from "node:test";
import { createServer } from "node:http";
import { WebSocketServer } from "ws";
import { Arker, ArkerError } from "../src/index.js";

test("private port preserves binary data, authentication and independent half-close", async () => {
  const server = new WebSocketServer({ port: 0 });
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address === "object");
  const client = new Arker({ apiKey: "ark_test", baseUrl: `http://127.0.0.1:${address.port}` });
  let received = Buffer.alloc(0);
  const connection = new Promise<void>((resolve, reject) => {
    server.on("connection", (ws, request) => {
      try {
        assert.equal(request.url, "/v1/vms/vm_test/ports/8080");
        assert.equal(request.headers.authorization, "Bearer ark_test");
        assert.equal(ws.protocol, "vm-port.v1");
      } catch (error) { reject(error); }
      ws.on("message", (data, binary) => {
        try {
          if (binary) {
            const bytes = Array.isArray(data) ? Buffer.concat(data) : Buffer.from(data as Buffer);
            assert(bytes.length <= 65536);
            received = Buffer.concat([received, bytes]);
          } else {
            assert.equal(data.toString(), "eof");
            ws.send(Buffer.from("eof"));
            ws.send(Buffer.from("response"));
            ws.send("eof");
            resolve();
          }
        } catch (error) { reject(error); }
      });
    });
  });
  try {
    const port = await client.vm("vm_test").connectPort(8080);
    const response: Buffer[] = [];
    const finished = new Promise<void>((resolve, reject) => {
      port.on("data", (data) => response.push(data));
      port.on("end", resolve);
      port.on("error", reject);
    });
    const payload = Buffer.from(Array.from({ length: 262144 }, (_, i) => i % 256));
    port.end(payload);
    await Promise.all([connection, finished]);
    assert.deepEqual(received, payload);
    assert.equal(Buffer.concat(response).toString(), "eofresponse");
    port.destroy();
  } finally {
    for (const socket of server.clients) socket.terminate();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test("private port rejects invalid ports before network access", async () => {
  const vm = new Arker({ apiKey: "ark_test", baseUrl: "https://example.invalid" }).vm("test");
  for (const port of [0, -1, 65536, 1.5, NaN]) {
    await assert.rejects(() => vm.connectPort(port), /port/);
  }
});

test("private port upgrade refuses redirects and preserves API errors without retry", async () => {
  for (const [status, chunked] of [[404, false], [302, false], [404, true]] as const) {
    let requests = 0;
    const server = createServer((_request, response) => {
      requests++;
      const body = JSON.stringify({ error: { code: "not_found", message: "missing" } });
      response.writeHead(status, { "Content-Type": "application/json", ...(!chunked ? { "Content-Length": Buffer.byteLength(body) } : {}), Location: "http://example.invalid/secret" });
      response.end(body);
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    assert(address && typeof address === "object");
    try {
      const vm = new Arker({ apiKey: "ark_test", baseUrl: `http://127.0.0.1:${address.port}` }).vm("vm");
      await assert.rejects(vm.connectPort(8080), (error: unknown) => {
        assert(error instanceof ArkerError);
        assert.equal(error.code, "not_found");
        assert.equal(error.status, status);
        return true;
      });
      assert.equal(requests, 1);
    } finally {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  }
});

test("private port rejects invalid frames and unannounced close", async () => {
  for (const kind of ["text", "oversized", "close", "after-eof"]) {
    const server = new WebSocketServer({ port: 0 });
    await once(server, "listening");
    const address = server.address();
    assert(address && typeof address === "object");
    server.on("connection", (socket) => {
      socket.on("error", () => {});
      if (kind === "close") socket.close();
      else if (kind === "oversized") socket.send(Buffer.alloc(65537));
      else if (kind === "after-eof") { socket.send("eof"); socket.send(Buffer.from("late")); }
      else socket.send("invalid");
    });
    try {
      const vm = new Arker({ apiKey: "ark_test", baseUrl: `http://127.0.0.1:${address.port}` }).vm("vm");
      await assert.rejects(async () => {
        const port = await vm.connectPort(8080);
        port.resume();
        const [error] = await once(port, "error");
        throw error;
      }, /frame|EOF|payload/i);
    } finally {
      for (const socket of server.clients) socket.terminate();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  }
});
