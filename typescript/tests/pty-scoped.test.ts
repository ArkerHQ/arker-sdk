import assert from "node:assert/strict";
import { test } from "node:test";
import { Arker } from "../src/index.js";

test("scoped PTY browser fallback keeps the API key out of the URL and worker headers", async () => {
  const calls: string[] = [];
  const client = new Arker({
    apiKey: "ark_scoped_secret",
    baseUrl: "https://test.invalid/api/",
    retry: false,
    fetch: async (input, init) => {
      const url = String(input);
      calls.push(`${init?.method} ${url}`);
      return new Response(JSON.stringify({ error: { code: "forbidden", message: "Forbidden" } }), {
        status: 403,
        headers: { "content-type": "application/json" },
      });
    },
  });
  let url = "";
  let protocols: string[] | undefined;
  let headers: Record<string, string> | undefined;
  await client.vm("vm1").connectPty({
    sessionId: "s1",
    useTicket: true,
    webSocketFactory: (wsUrl, init) => {
      url = wsUrl;
      protocols = init.protocols;
      headers = init.headers;
      return { readyState: 1, send() {}, close() {}, addEventListener() {} };
    },
  });
  assert.deepEqual(calls, ["POST https://test.invalid/api/v1/vms/vm1/sessions/s1/pty-ticket"]);
  assert.equal(url, "wss://test.invalid/api/v1/vms/vm1/sessions/s1/pty");
  assert.deepEqual(protocols, ["arker-pty-key.ark_scoped_secret", "arker-pty"]);
  assert.equal(headers, undefined);
});

test("browser WebSocket receives the scoped-key subprotocol", async () => {
  const original = globalThis.WebSocket;
  let offered: string | string[] | undefined;
  class BrowserSocket {
    readyState = 1;
    send() {}
    close() {}
    addEventListener() {}
    constructor(_url: string, protocols?: string | string[]) {
      offered = protocols;
    }
  }
  globalThis.WebSocket = BrowserSocket as unknown as typeof WebSocket;
  try {
    const client = new Arker({
      apiKey: "ark_scoped_secret",
      baseUrl: "https://test.invalid/api/",
      retry: false,
      fetch: async () => new Response(JSON.stringify({ error: { code: "forbidden", message: "Forbidden" } }), {
        status: 403,
        headers: { "content-type": "application/json" },
      }),
    });
    await client.vm("vm1").connectPty({ sessionId: "s1", useTicket: true });
    assert.deepEqual(offered, ["arker-pty-key.ark_scoped_secret", "arker-pty"]);
  } finally {
    globalThis.WebSocket = original;
  }
});
