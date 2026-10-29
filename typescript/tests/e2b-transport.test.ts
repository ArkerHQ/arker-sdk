import assert from "node:assert/strict";
import { test } from "node:test";
import { Arker } from "../src/index.js";
import { configureTransport, finishInitialization, requestTimeout, requestTransport } from "../src/compat/e2b-transport.js";
import { E2BLifecycle } from "../src/compat/e2b-lifecycle.js";
import type { UniversalSandbox } from "../src/compat/common.js";

function fixture() {
  const seen: RequestInit[] = [];
  const client = new Arker({ apiKey: "arker-only", baseUrl: "https://example.invalid", retry: false,
    fetch: (async (_url, options) => {
      seen.push(options!);
      options?.signal?.throwIfAborted();
      return new Response(JSON.stringify({ vm_id: "vm", state: "running", customer_paused: false, mode: null, deleted: true }));
    }) as typeof fetch });
  return { client, seen, vm: client.vm("vm") };
}

test("request deadlines are fresh per request, private to a client, and zero disables them", async () => {
  const { client, seen, vm } = fixture();
  configureTransport(client, 20);
  await client._request("GET", "/one");
  const first = seen[0]!.signal!;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(first.aborted, true);
  await client._request("GET", "/two");
  assert.notEqual(seen[1]!.signal, first);
  assert.equal(seen[1]!.signal!.aborted, false);
  assert.equal((seen[0]!.headers as Record<string, string>).authorization, "Bearer arker-only");
  configureTransport(client, 0);
  await client._request("GET", "/unlimited");
  assert.equal(seen[2]!.signal, undefined);
  assert.equal(requestTimeout(vm), 0);
  assert.equal(requestTimeout(fixture().vm), 60_000);
});

test("lifecycle inherits defaults and per-call overrides do not mutate them", async () => {
  const { client, seen, vm } = fixture();
  configureTransport(client, 0);
  const box = { provider: "arker", getInstance: () => vm } as unknown as UniversalSandbox;
  const helper = new E2BLifecycle(box);
  await helper.isRunning();
  await helper.isRunning({ requestTimeoutMs: 1000 });
  await helper.isRunning();
  assert.equal(seen[0]!.signal, undefined);
  assert.ok(seen[1]!.signal);
  assert.equal(seen[2]!.signal, undefined);
});

test("initialization cancellation cannot poison cleanup or the returned handle", async () => {
  const { client, seen, vm } = fixture();
  const controller = new AbortController();
  configureTransport(client, 0, controller.signal);
  const reason = new Error("cancel initialization");
  controller.abort(reason);
  await assert.rejects(client._request("POST", "/prepare"), error => error === reason);
  assert.equal(seen.length, 0);
  finishInitialization(vm);
  await vm.delete();
  assert.equal(seen.length, 1);
  assert.equal(seen[0]!.signal, undefined);
});

test("zero retains caller cancellation while invalid timeout values are rejected", () => {
  const controller = new AbortController();
  assert.equal(requestTransport({ requestTimeoutMs: 0, signal: controller.signal }).signal, controller.signal);
  for (const requestTimeoutMs of [-1, NaN, Infinity, 0.5, true, "10"]) {
    assert.throws(() => requestTransport({ requestTimeoutMs: requestTimeoutMs as number }), /requestTimeoutMs/);
  }
});

test("presigned downloads inherit timeout and cancellation without authentication headers", async () => {
  const { client, seen } = fixture();
  configureTransport(client, 1000);
  await client._download("https://presigned.invalid/file");
  assert.ok(seen[0]!.signal);
  assert.equal(seen[0]!.headers, undefined);
  configureTransport(client, 0);
  await client._download("https://presigned.invalid/file");
  assert.equal(seen[1]!.signal, undefined);
  const controller = new AbortController();
  configureTransport(client, 0, controller.signal);
  const reason = new Error("cancel download");
  controller.abort(reason);
  await assert.rejects(client._download("https://presigned.invalid/file"), error => error === reason);
  assert.equal(seen[2]!.headers, undefined);
});

test("real HTTP request overrides are not capped by the client default", async () => {
  const { createServer } = await import("node:http");
  const { listen, closeServer } = await import("./provider-fixtures.js");
  const received: Array<{ path: string; authorization?: string }> = [];
  const server = createServer((request, response) => {
    received.push({ path: request.url!, authorization: request.headers.authorization });
    setTimeout(() => { response.setHeader("content-type", "application/json"); response.end('{"ok":true}'); }, 80);
  });
  const address = await listen(server);
  const baseUrl = `http://127.0.0.1:${address.port}`;
  const client = new Arker({ apiKey: "arker-only", baseUrl, retry: false });
  configureTransport(client, 20);
  try {
    await assert.rejects(client._request("GET", "/default"), /timeout|aborted/i);
    const unlimited = requestTransport({ requestTimeoutMs: 0 });
    const longer = requestTransport({ requestTimeoutMs: 1000 });
    assert.deepEqual(await client._request("GET", "/zero", undefined, baseUrl, undefined, undefined, undefined, unlimited), { ok: true });
    assert.deepEqual(await client._request("GET", "/longer", undefined, baseUrl, undefined, undefined, undefined, longer), { ok: true });
    // The raw fetch primitive must preserve the caller's signal without adding a default timer.
    assert.deepEqual(await (await client._fetch(`${baseUrl}/raw`, { signal: unlimited.signal })).json(), { ok: true });
    assert.deepEqual(await (await client._download(`${baseUrl}/signed`, unlimited)).json(), { ok: true });
    await assert.rejects(client._download(`${baseUrl}/signed-default`), /timeout|aborted/i);
    assert.ok(received.filter(value => ["/zero", "/longer"].includes(value.path)).every(value => value.authorization === "Bearer arker-only"));
    assert.ok(received.filter(value => value.path.startsWith("/signed") || value.path === "/raw").every(value => value.authorization === undefined));
    assert.equal(requestTimeout(client.vm("vm")), 20);
  } finally { await closeServer(server); }
});
