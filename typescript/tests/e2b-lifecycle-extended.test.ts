import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import { Sandbox as VendorSandbox } from "e2b";
import { Sandbox } from "../src/e2b.js";
import { preserveEnvironment, preserveProperties, listen, closeServer } from "./provider-fixtures.js";
import { SandboxError, InvalidArgumentError, SandboxNotFoundError, SandboxNotRunningError, type Sandbox as NativeSandbox } from "e2b";
import { ArkerError, type VM, type PolicyUpdate } from "../src/index.js";
import { E2BLifecycle, E2B_LIFETIME_POLICY_ID as lifetime, validateArkerE2BLifecycle, e2bTimeoutDuration } from "../src/compat/e2b-lifecycle.js";
import type { UniversalSandbox } from "../src/compat/common.js";

function fixture() {
  const rules: NonNullable<NonNullable<VM["policies"]>["rules"]> = {
    [lifetime]: { type: "schedule", after_created: 300, action: "delete" },
    independent: { type: "schedule", after_created: 3600, action: "suspend" },
    network: { type: "outbound", action: "deny", priority: 1 },
  };
  const policies = { rules, secrets: { token: "***" }, outbound_guards: { guard: { type: "outbound_guard" as const, allow: [] } } };
  const writes: PolicyUpdate[] = [];
  let reads = 0;
  const current = {
    state: "running", created_at: new Date().toISOString(), policies,
    update: async ({ policies: patch }: { policies: PolicyUpdate }) => {
      writes.push(structuredClone(patch));
      for (const [id, rule] of Object.entries(patch.rules ?? {})) {
        if (rule === null) delete rules[id];
        else rules[id] = rule as typeof rules[string];
      }
    },
  };
  const requests: Array<{ method: string; path: string; body: unknown; baseUrl: string }> = [];
  const state = { vm_id: "test-sandbox", state: "running", mode: null as string | null, customer_paused: false };
  const handle = {
    id: "test-sandbox", baseUrl: "https://worker.example",
    _client: { _request: async (method: string, path: string, body: any, baseUrl: string): Promise<Record<string, unknown>> => {
      requests.push({ method, path, body, baseUrl });
      if (method === "GET") return state;
      if (path.endsWith("/deadline")) {
        if (state.customer_paused) throw new ArkerError("invalid_state", "paused", 409,
          { code: "invalid_state", details: { resource: { kind: "vm", id: handle.id }, requirement: "explicit_resume" } } as any);
        return { vm_id: handle.id, deadline_at: new Date(Date.now() + body.timeout_seconds * 1000).toISOString() };
      }
      return { vm_id: handle.id, state: path.endsWith("/pause") ? "paused" : "running", mode: body.mode ?? "full", already_paused: false };
    } },
    refresh: async () => { reads++; return current; },
  };
  const box = { provider: "arker", sandboxId: "test-sandbox", getInstance: () => handle } as unknown as UniversalSandbox;
  return { helper: new E2BLifecycle(box), box, current, handle, writes, rules, requests, state, get reads() { return reads; } };
}

function unsupported(error: unknown): boolean {
  return error instanceof ArkerError && error.code === "unsupported_operation";
}

test("running timeout uses the atomic active-only endpoint, including reductions", async () => {
  const f = fixture();
  const before = structuredClone(f.current.policies);
  for (const timeout of [60_000, 1001]) await f.helper.setTimeout(timeout);
  assert.deepEqual(f.requests.map(call => [call.method, call.path, call.body]), [60, 2].map(seconds => [
    "POST", "/v1/vms/test-sandbox/deadline", { policy_id: lifetime, timeout_seconds: seconds, max_session_duration_seconds: 86_400 },
  ]));
  assert.deepEqual(f.current.policies, before);
  assert.deepEqual(f.writes, []);
  assert.equal(f.reads, 0);
});

test("connect delegates deadline arithmetic atomically including default and absent rule", async () => {
  const f = fixture();
  for (const timeoutMs of [undefined, 1000, 600_000]) {
    await f.helper.connect({ timeoutMs });
    assert.deepEqual(f.requests.at(-1)?.body, { deadline: { policy_id: lifetime, timeout_seconds: (timeoutMs ?? 300_000) / 1000 } });
  }
  delete f.rules[lifetime];
  await f.helper.connect();
  assert.deepEqual(f.requests.at(-1)?.body, { deadline: { policy_id: lifetime, timeout_seconds: 300 } });
  assert.equal(f.writes.length, 0);
});

test("atomic deadline refusals never fall back to a policy PATCH or resume", async () => {
  const f = fixture();
  const failure = new ArkerError("unsupported_operation", "selected policy is incompatible", 422);
  f.handle._client._request = async () => { throw failure; };
  await assert.rejects(f.helper.setTimeout(1000), error => error === failure);
  assert.equal(f.writes.length, 0);
  assert.equal(f.reads, 0);
});

test("invalid timeout and unsupported transport options fail before I/O", async () => {
  const f = fixture();
  const badRequest = (error: unknown) => error instanceof SandboxError && error.constructor === SandboxError && error.statusCode === 400;
  for (const timeout of [-1000, NaN, Infinity]) {
    await assert.rejects(f.helper.setTimeout(timeout), badRequest);
  }
  for (const timeout of [-1000, 0, NaN, Infinity, 86_401_000]) {
    await assert.rejects(f.helper.connect({ timeoutMs: timeout }), badRequest);
    assert.throws(() => e2bTimeoutDuration(timeout), badRequest);
  }
  await assert.rejects(f.helper.setTimeout(1000, { domain: "example.invalid" } as any), /unsupported option/);
  await assert.rejects(f.helper.isRunning({ apiKey: "test" } as any), /unsupported option/);
  assert.equal(f.reads, 0);
  assert.equal(f.writes.length, 0);
});

test("pause uses explicit lifecycle routes and never changes policy", async () => {
  const f = fixture();
  delete f.rules[lifetime];
  const before = structuredClone(f.current.policies);
  for (const [options, mode] of [[undefined, "full"], [{ mode: "filesystem" }, "filesystem"], [{ keepMemory: false }, "filesystem"]] as const) {
    assert.equal(await f.helper.pause(options), true);
    assert.deepEqual(f.requests.at(-1), { method: "POST", path: "/v1/vms/test-sandbox/pause", body: { mode, deadline: { policy_id: lifetime } }, baseUrl: "https://worker.example" });
  }
  f.handle._client._request = async () => ({ vm_id: "test-sandbox", state: "paused", mode: "full", already_paused: true });
  assert.equal(await f.helper.betaPause(), false);
  assert.deepEqual(f.current.policies, before);
  assert.equal(f.writes.length, 0);
});

test("connect defaults to durable mode and only reboot requests filesystem", async () => {
  const f = fixture();
  f.current.state = "idle";
  await f.helper.connect();
  await f.helper.connect({ onResume: "reboot" });
  assert.deepEqual(f.requests.map(call => [call.path, call.body]), [
    ["/v1/vms/test-sandbox/resume", { deadline: { policy_id: lifetime, timeout_seconds: 300 } }], ["/v1/vms/test-sandbox/resume", { mode: "filesystem", deadline: { policy_id: lifetime, timeout_seconds: 300 } }],
  ]);
  f.state.state = "paused"; f.state.mode = "full"; f.state.customer_paused = true;
  await assert.rejects(f.helper.setTimeout(1000), SandboxNotFoundError);
  assert.equal(f.writes.length, 0);
});

test("invalid pause options and responses cannot report success", async () => {
  const f = fixture();
  await assert.rejects(f.helper.pause({ mode: "full", keepMemory: true }), InvalidArgumentError);
  await assert.rejects(f.helper.pause({ mode: "invalid" } as any), InvalidArgumentError);
  await assert.rejects(f.helper.pause({ domain: "example.invalid" } as any), /unsupported option/);
  assert.equal(f.requests.length, 0);
  delete f.rules[lifetime];
  for (const response of [{}, { vm_id: "other", state: "paused", mode: "full" },
      { vm_id: "test-sandbox", state: "paused", mode: "full", already_paused: "false" }]) {
    f.handle._client._request = async () => response;
    await assert.rejects(f.helper.pause(), /Invalid Arker/);
  }
});

test("running checks require authoritative lifecycle state without waking the guest", async () => {
  const f = fixture();
  assert.equal(await f.helper.isRunning(), true);
  f.state.state = "paused";
  f.state.mode = "full";
  f.state.customer_paused = true;
  assert.equal(await f.helper.isRunning(), false);
  f.state.state = "idle";
  await assert.rejects(f.helper.isRunning(), /Inconsistent/);
  f.state.customer_paused = false;
  for (const mode of [null, "full", "filesystem"]) {
    f.state.mode = mode;
    assert.equal(await f.helper.isRunning(), true);
  }
  f.state.state = "paused";
  await assert.rejects(f.helper.isRunning(), /Inconsistent/);
  assert.equal(f.reads, 0);
  assert.ok(f.requests.every(call => call.method === "GET" && call.path.endsWith("/lifecycle")));
});

test("lifecycle route errors including missing routes never fall back or report paused", async () => {
  for (const status of [402, 403, 404, 409, 422, 503]) {
    for (const method of ["pause", "connect", "isRunning"] as const) {
      const f = fixture();
      delete f.rules[lifetime];
      const failure = new ArkerError("request_failed", "request failed", status);
      let calls = 0;
      f.handle._client._request = async () => { calls++; throw failure; };
      if (status === 404 && method === "isRunning") assert.equal(await f.helper.isRunning(), false);
      else await assert.rejects(f.helper[method](), (error) => status === 404 ? error instanceof SandboxNotFoundError : error === failure);
      assert.equal(calls, 1);
      assert.equal(f.writes.length, 0);
    }
  }
});

test("info refuses to invent native fields or treat idle as E2B paused", async () => {
  const f = fixture();
  await assert.rejects(f.helper.getInfo(), unsupported);
  f.current.state = "idle";
  await assert.rejects(f.helper.getInfo(), unsupported);
  assert.equal(f.writes.length, 0);
});

test("native lifecycle preserves options, receiver, results, and error identity", async () => {
  const f = fixture();
  const calls: unknown[][] = [];
  const info = { startedAt: new Date(), endAt: new Date(), state: "paused", metadata: { purpose: "test" } };
  const native = {
    async pause(opts: unknown) { assert.equal(this, native); calls.push(["pause", opts]); return false; },
    async connect(opts: unknown) { assert.equal(this, native); calls.push(["connect", opts]); return this; },
    async setTimeout(ms: number, opts: unknown) { assert.equal(this, native); calls.push(["timeout", ms, opts]); },
    async isRunning(opts: unknown) { calls.push(["running", opts]); return false; },
    async getInfo(opts: unknown) { calls.push(["info", opts]); return info; },
  };
  const helper = new E2BLifecycle(f.box, native as unknown as NativeSandbox);
  const opts = { requestTimeoutMs: 1000, signal: new AbortController().signal };
  const pause = { ...opts, mode: "filesystem" as const };
  const connect = { ...opts, onResume: "reboot" as const, timeoutMs: 9000 };
  assert.equal(await helper.pause(pause), false);
  assert.equal(await helper.connect(connect), helper);
  assert.equal(await helper.setTimeout(9000, opts), undefined);
  assert.equal(await helper.isRunning(opts), false);
  assert.equal(await helper.getInfo(opts), info);
  assert.deepEqual(calls, [["pause", pause], ["connect", connect], ["timeout", 9000, opts], ["running", opts], ["info", opts]]);
  assert.equal(f.reads, 0);
  const failure = new SandboxNotFoundError("missing");
  native.pause = async () => { throw failure; };
  await assert.rejects(helper.pause(), (error) => error === failure);
});

test("creation preflight accepts kill and rejects unsupported auto-pause", () => {
  validateArkerE2BLifecycle();
  validateArkerE2BLifecycle({ onTimeout: "kill" });
  validateArkerE2BLifecycle({ onTimeout: { action: "kill" }, autoResume: false });
  assert.throws(() => validateArkerE2BLifecycle({ onTimeout: "kill", autoResume: true }), InvalidArgumentError);
  for (const mode of ["full", "filesystem"] as const) {
    for (const autoResume of [true, false]) {
      assert.throws(() => validateArkerE2BLifecycle({ onTimeout: { action: "pause", mode }, autoResume }), unsupported);
    }
  }
});


test("instance connect resumes before data-plane replacement", async (t) => {
  const factory = Sandbox as unknown as { dataPlane: () => Promise<unknown> };
  const original = factory.dataPlane;
  t.after(() => { factory.dataPlane = original; });
  const f = fixture();
  const events: string[] = [];
  const request = f.handle._client._request;
  f.handle._client._request = async (...args) => { events.push("resume"); return request(...args); };
  const box = { ...f.box, runCommand: async () => {
    events.push("identity");
    return { stdout: "", stderr: "", exitCode: 0 };
  } };
  const replacement = { commands: {}, files: {}, pty: {}, git: {}, close() {} };
  factory.dataPlane = async () => { events.push("data-plane"); return replacement; };
  const previous = { close() { events.push("close"); } };
  const sandbox = Reflect.construct(Sandbox, [box, undefined, undefined, previous]) as Sandbox;
  assert.equal(await sandbox.connect(), sandbox);
  assert.equal(sandbox.commands, replacement.commands);
  assert.deepEqual(events, ["resume", "data-plane", "close"]);
});

test("static connect never validates identity or cold boots after failed resume", async (t) => {
  const id = "vmh-0000000000000000-00000000000000000000000000";
  const paths: string[] = [];
  const server = createServer(async (request, response) => {
    paths.push(`${request.method} ${request.url}`);
    response.setHeader("content-type", "application/json");
    if (request.url === `/api/v1/vms/${id}` && request.method === "GET") {
      response.end(JSON.stringify({ vm_id: id, owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z",
        public: false, state: "idle", sessions: [], network: {}, resources: {} }));
    } else if (request.url === `/api/v1/vms/${id}/resume` && request.method === "POST") {
      let body = "";
      for await (const chunk of request) body += chunk;
      assert.deepEqual(JSON.parse(body), { deadline: { policy_id: lifetime, timeout_seconds: 300 } });
      response.writeHead(409);
      response.end(JSON.stringify({ error: { code: "conflict", message: "missing checkpoint", resource: "vm" } }));
    } else {
      response.writeHead(500);
      response.end(JSON.stringify({ error: { code: "internal", message: "unexpected guest access" } }));
    }
  });
  const address = await listen(server);
  t.after(() => closeServer(server));
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_BASE_URL", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "ark_test";
  process.env.ARKER_BASE_URL = `http://127.0.0.1:${address.port}/api`;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  await assert.rejects(Sandbox.connect(id), (error) => error instanceof ArkerError && error.status === 409);
  assert.ok(paths.includes(`POST /api/v1/vms/${id}/resume`));
  assert.ok(paths.every(path => path === `GET /api/v1/vms/${id}` || path === `POST /api/v1/vms/${id}/resume`));
  assert.equal(paths.filter(path => path.startsWith("POST")).length, 1);
});

test("static native connect preserves lifecycle and transport options", async (t) => {
  preserveProperties(t, VendorSandbox, "getInfo", "connect");
  preserveEnvironment(t, "ARKER_API_KEY", "E2B_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  process.env.E2B_API_KEY = "e2b_test";
  VendorSandbox.getInfo = async () => ({ sandboxId: "native" }) as any;
  const options = { timeoutMs: 42000, onResume: "reboot" as const, requestTimeoutMs: 1200 };
  VendorSandbox.connect = (async (id: string, actual: unknown) => {
    assert.equal(id, "native");
    assert.deepEqual(actual, options);
    return { sandboxId: id, commands: {}, files: {}, git: {} };
  }) as typeof VendorSandbox.connect;
  assert.equal((await Sandbox.connect("native", options)).sandboxId, "native");
});


test("reserved lifetime selects atomic pause for all modes and clocks", async () => {
  for (const clock of [{ at: new Date(0).toISOString() }, { after_created: 300 }]) {
    for (const method of ["pause", "betaPause"] as const) {
      for (const options of [undefined, { mode: "full" as const }, { mode: "filesystem" as const }, { keepMemory: false }]) {
        const f = fixture();
        f.rules[lifetime] = { type: "schedule", action: "delete", ...clock };
        const before = structuredClone(f.current.policies);
        assert.equal(await f.helper[method](options), true);
        assert.deepEqual((f.requests[0].body as any).deadline, { policy_id: lifetime });
        assert.equal(f.writes.length, 0);
        assert.deepEqual(f.current.policies, before);
      }
    }
  }
});

test("pause delegates policy selection and preserves unrelated explicit delete rules", async () => {
  const f = fixture();
  const fresh = { ...f.current, policies: structuredClone(f.current.policies) };
  delete f.rules[lifetime];
  f.handle.refresh = async () => fresh;
  assert.equal(await f.helper.pause(), true);
  assert.equal(f.requests.length, 1);
  f.handle.refresh = async () => f.current;
  f.rules["user-deadline"] = { type: "schedule", action: "delete", after_created: 600 };
  const before = structuredClone(f.current.policies);
  assert.equal(await f.helper.pause(), true);
  assert.deepEqual(f.current.policies, before);
  assert.equal(f.writes.length, 0);
});


test("paused timeout never recreates a removed deadline, even with a stale running VM", async () => {
  const f = fixture();
  delete f.rules[lifetime];
  f.state.state = "paused"; f.state.mode = "full"; f.state.customer_paused = true;
  await assert.rejects(f.helper.setTimeout(1000), SandboxNotFoundError);
  assert.equal(f.reads, 0);
  assert.deepEqual(f.writes, []);
  assert.equal(f.rules[lifetime], undefined);
});

test("deleted lifecycle state is false but info and timeout raise native not-found", async () => {
  const f = fixture();
  const missing = new ArkerError("not_found", "missing", 404);
  f.handle._client._request = async () => { throw missing; };
  f.handle.refresh = async () => { throw missing; };
  assert.equal(await f.helper.isRunning(), false);
  await assert.rejects(f.helper.setTimeout(1000), SandboxNotFoundError);
  await assert.rejects(f.helper.getInfo(), SandboxNotFoundError);
  assert.deepEqual(f.writes, []);
});


test("repeated pause returns false even when the persisted mode differs", async () => {
  const f = fixture();
  f.handle._client._request = async () => ({ vm_id: "test-sandbox", state: "paused", mode: "filesystem", already_paused: true });
  assert.equal(await f.helper.pause({ mode: "full" }), false);
  assert.equal(f.writes.length, 0);
});


test("atomic timeout maps only VM explicit-resume refusal and validates success", async () => {
  const f = fixture();
  for (const failure of [new ArkerError("unavailable", "temporarily unavailable", 503), new ArkerError("invalid_state", "other state", 409), new SandboxNotRunningError("unrelated native failure")]) {
    f.handle._client._request = async () => { throw failure; };
    await assert.rejects(f.helper.setTimeout(1000), error => error === failure);
  }
  for (const response of [{}, { vm_id: "other", deadline_at: new Date().toISOString() }, { vm_id: "test-sandbox", deadline_at: "invalid" }]) {
    f.handle._client._request = async () => response;
    await assert.rejects(f.helper.setTimeout(1000), /Invalid Arker deadline/);
  }
  assert.deepEqual(f.writes, []);
});

test("per-call transport cancels requests and retry waits without affecting concurrent calls", async () => {
  const { Arker } = await import("../src/index.js");
  let attempts = 0;
  const client = new Arker({ apiKey: "test", baseUrl: "https://example.invalid",
    fetch: async (_url, init) => {
      attempts++;
      if (attempts === 1) return new Response(JSON.stringify({ error: { code: "unavailable", message: "retry" } }), { status: 503 });
      return await new Promise<Response>((resolve, reject) => {
        const timer = setTimeout(() => resolve(new Response(JSON.stringify({ vm_id: "test", state: "running", mode: null, customer_paused: false }))), 40);
        const abort = () => { clearTimeout(timer); reject(init?.signal?.reason); };
        init?.signal?.addEventListener("abort", abort, { once: true });
        if (init?.signal?.aborted) abort();
      });
    },
  });
  const vm = client.vm("test");
  const helper = new E2BLifecycle({ provider: "arker", sandboxId: "test", getInstance: () => vm } as unknown as UniversalSandbox);
  await assert.rejects(helper.isRunning({ requestTimeoutMs: 5 }), error => error instanceof DOMException && error.name === "TimeoutError");
  assert.equal(attempts, 1, "timeout during backoff must prevent retry");
  const controller = new AbortController();
  const failure = new Error("caller cancelled");
  const cancelled = helper.isRunning({ requestTimeoutMs: 0, signal: controller.signal });
  const completed = helper.isRunning({ requestTimeoutMs: 0 });
  controller.abort(failure);
  await assert.rejects(cancelled, error => error === failure);
  assert.equal(await completed, true);
  const before = attempts;
  await assert.rejects(helper.isRunning({ signal: controller.signal }), error => error === failure);
  assert.equal(attempts, before, "pre-aborted request must not send");
});

test("HTTP/2 cancellation closes only its stream and keeps the shared connection usable", async () => {
  const { createServer } = await import("node:http2");
  const { Arker } = await import("../src/index.js");
  const server = createServer();
  const sessions = new Set<import("node:http2").ServerHttp2Session>();
  server.on("session", session => { sessions.add(session); session.on("close", () => sessions.delete(session)); });
  let closed!: () => void;
  const cancelledStreamClosed = new Promise<void>(resolve => { closed = resolve; });
  server.on("stream", (stream: import("node:http2").ServerHttp2Stream, headers) => {
    stream.on("error", () => {});
    if (String(headers[":path"]).includes("/slow/")) { stream.on("close", closed); return; }
    stream.respond({ ":status": 200, "content-type": "application/json" });
    stream.end(JSON.stringify({ vm_id: "fast", state: "running", mode: null, customer_paused: false }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const client = new Arker({ apiKey: "test", baseUrl: `http://127.0.0.1:${(server.address() as import("node:net").AddressInfo).port}`, retry: false });
  const helper = (id: string) => new E2BLifecycle({ provider: "arker", sandboxId: id, getInstance: () => client.vm(id) } as unknown as UniversalSandbox);
  try {
    assert.equal(await helper("fast").isRunning(), true);
    await assert.rejects(helper("slow").isRunning({ requestTimeoutMs: 20 }), error => error instanceof DOMException && error.name === "TimeoutError");
    await cancelledStreamClosed;
    assert.equal(await helper("fast").isRunning({ requestTimeoutMs: 0 }), true);
    assert.equal(sessions.size, 1);
  } finally {
    for (const session of sessions) session.destroy();
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});

test("zero and oversized setters delegate expiration and session cap without GET arithmetic", async () => {
  const f = fixture();
  for (const timeout of [0, 86_401_000]) await f.helper.setTimeout(timeout);
  assert.deepEqual(f.requests.map(call => call.body), [0, 86_401].map(seconds => ({
    policy_id: lifetime, timeout_seconds: seconds, max_session_duration_seconds: 86_400,
  })));
  assert.equal(f.reads, 0);
  assert.equal(f.writes.length, 0);
});
