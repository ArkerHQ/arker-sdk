import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";
import {
  Sandbox as Native,
  NotFoundError,
  SandboxError,
  CommandExitError,
  FileType,
} from "e2b";
import { Sandbox } from "../src/e2b.js";
import { Arker, ArkerError, type VM } from "../src/index.js";
import { rememberTrafficToken } from "../src/compat/e2b-network.js";
import { createArkerComputeProvider } from "../src/compat/arker-provider.js";
import { createNativeE2BProvider } from "../src/compat/e2b-provider.js";
import { preserveProperties, preserveEnvironment, listen, closeServer } from "./provider-fixtures.js";

test("native filesystem handles preserve binary options and write metadata", async () => {
  const marker = { path: "/binary", name: "binary" };
  class NativeFiles {
    async read(path: string, options: unknown) {
      assert.equal(path, "/binary");
      assert.deepEqual(options, { format: "bytes", user: "user", requestTimeoutMs: 7000 });
      return new Uint8Array([0, 255, 128]);
    }
    async write(path: string, content: unknown, options: unknown) {
      assert.equal(path, "/binary");
      assert.deepEqual(content, new Uint8Array([255]).buffer);
      assert.deepEqual(options, { user: "user" });
      return marker;
    }
  }
  const files = new NativeFiles();
  const sandbox = Reflect.construct(Sandbox, [
    { provider: "e2b", sandboxId: "native", filesystem: { readFile: async () => "translated" } },
    { files },
  ]) as Sandbox;
  assert.equal(sandbox.files, files);
  assert.deepEqual(await sandbox.files.read("/binary", { format: "bytes", user: "user", requestTimeoutMs: 7000 }), new Uint8Array([0, 255, 128]));
  assert.equal(await sandbox.files.write("/binary", new Uint8Array([255]).buffer, { user: "user" }), marker);
});

test("the shim provider preserves embedding SDK identity, native filesystem metadata and errors", async (t) => {
  preserveProperties(t, Native, "create", "connect");
  const modifiedTime = new Date("2026-10-17T00:00:00Z");
  const failure = new CommandExitError({
    stdout: "output",
    stderr: "failure",
    exitCode: 17,
    error: "exit status 17",
  });
  const native = {
    sandboxId: "embedded",
    commands: {
      run: async () => {
        throw failure;
      },
    },
    files: {
      list: async () => [
        { name: "folder", type: FileType.DIR, size: 4096, modifiedTime },
      ],
    },
  } as unknown as Native;
  Native.create = (async () => native) as typeof Native.create;
  Native.connect = (async () => native) as typeof Native.connect;
  const provider = createNativeE2BProvider();
  const box = await provider.instances.create();
  assert.equal(box.provider, "e2b");
  assert.equal(box.getInstance(), native);
  assert.equal(
    (await provider.instances.getById("embedded"))!.getInstance(),
    native,
  );
  assert.deepEqual(await box.filesystem.readdir("/"), [
    { name: "folder", type: "directory", size: 4096, modified: modifiedTime },
  ]);
  const result = await box.runCommand("exit 17");
  assert.deepEqual(
    [result.stdout, result.stderr, result.exitCode],
    ["output", "failure", 17],
  );
  const unavailable = new Error("fetch failed");
  for (const [method, operation] of [
    ["create", () => provider.instances.create()],
    ["connect", () => provider.instances.getById("embedded")],
  ] as const) {
    Native[method] = async () => {
      throw unavailable;
    };
    await assert.rejects(operation(), (error) => error === unavailable);
  }
  Native.connect = async () => {
    throw new NotFoundError("missing");
  };
  assert.equal(await provider.instances.getById("missing"), null);
});

test("static E2B deletion uses metadata ownership without connecting and preserves errors", async (t) => {
  preserveProperties(t, Native, "getInfo", "kill", "connect");
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  let probes = 0;
  let deletes = 0;
  const unavailable = new Error("forbidden");
  Native.connect = async () => {
    throw new Error("Deletion must not connect");
  };
  Native.getInfo = async () => {
    probes++;
    if (probes === 2) throw new NotFoundError("missing");
    if (probes === 3) throw unavailable;
    return {} as Awaited<ReturnType<typeof Native.getInfo>>;
  };
  Native.kill = async (id) => {
    assert.equal(id, "native-id");
    deletes++;
    return true;
  };
  assert.equal(await Sandbox.kill("native-id"), true);
  assert.equal(await Sandbox.kill("native-id"), false);
  await assert.rejects(
    Sandbox.kill("native-id"),
    (error) => error === unavailable,
  );
  assert.equal(deletes, 1);
});

test("static Arker deletion translates idempotent DELETE without probing incompatible native IDs", async (t) => {
  const id = "vmh-b471cad989f2eab9-01M548DGAXJ48SGE3SRTRR8HNV";
  let deleted = false;
  let deletes = 0;
  const server = createServer((request, response) => {
    assert.equal(request.headers.authorization, "Bearer ark_test");
    assert.equal(request.url, `/api/v1/vms/${id}`);
    response.setHeader("content-type", "application/json");
    if (request.method === "DELETE") {
      deleted = true;
      deletes++;
      response.end(JSON.stringify({ deleted: true }));
    } else if (deleted) {
      response.writeHead(404);
      response.end(
        JSON.stringify({ error: { code: "not_found", message: "missing" } }),
      );
    } else {
      response.end(
        JSON.stringify({
          vm_id: id,
          owner_org_id: "owner",
          created_at: "2026-10-17T00:00:00Z",
          public: false,
          state: "idle",
          sessions: [],
          network: {},
          resources: {},
          hostname: "vm.example.invalid",
        }),
      );
    }
  });
  const address = await listen(server);
  preserveProperties(t, Native, "getInfo");
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_BASE_URL", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "ark_test";
  process.env.ARKER_BASE_URL = `http://127.0.0.1:${address.port}/api`;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  Native.getInfo = async () => {
    throw new Error("Arker IDs must not be sent to E2B");
  };
  try {
    assert.equal(await Sandbox.kill(id), true);
    assert.equal(await Sandbox.kill(id), false);
    assert.equal(deletes, 1);
  } finally {
    await closeServer(server);
  }
});

test("native creation forwards vendor options after routing", async t => {
  preserveProperties(t, Native, "create");
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY; delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const options = { apiKey: "native-test-key", domain: "example.invalid", secure: true,
    lifecycle: { onTimeout: "pause" as const, autoResume: true }, requestTimeoutMs: 19,
    headers: { "x-test": "native" }, signal: new AbortController().signal, timeoutMs: 1234 };
  Native.create = (async (template: unknown, actual: unknown) => {
    assert.equal(template, "native-template"); assert.deepEqual(actual, options);
    return { sandboxId: "native-created" } as Native;
  }) as typeof Native.create;
  assert.equal((await Sandbox.create("native-template", options)).sandboxId, "native-created");
  Native.create = (async (actual: unknown) => { assert.deepEqual(actual, {}); return { sandboxId: "native-default" } as Native; }) as typeof Native.create;
  await Sandbox.create();
});

test("static lifecycle aliases forward native options without connecting", async t => {
  preserveProperties(t, Native, "getInfo", "pause", "setTimeout", "connect", "kill");
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY; delete process.env.COMPUTESDK_ROUTER_CONFIG;
  Native.getInfo = async (_id, actual) => { assert.deepEqual(actual, options); return {} as Awaited<ReturnType<typeof Native.getInfo>>; };
  Native.connect = async () => { throw new Error("must not resume"); };
  const options = { apiKey: "native-test", requestTimeoutMs: 0, signal: new AbortController().signal };
  let pauses = 0;
  Native.pause = async (id, actual) => { assert.equal(id, "native"); assert.deepEqual(actual, { ...options, mode: "filesystem" }); pauses++; return true; };
  Native.setTimeout = async (id, duration, actual) => { assert.equal(id, "native"); assert.equal(duration, 1234); assert.deepEqual(actual, options); };
  Native.kill = async (_id, actual) => { assert.deepEqual(actual, options); return true; };
  assert.equal(await Sandbox.pause("native", { ...options, mode: "filesystem" }), true);
  assert.equal(await Sandbox.betaPause("native", { ...options, mode: "filesystem" }), true);
  await Sandbox.setTimeout("native", 1234, options);
  assert.equal(await Sandbox.kill("native", options), true);
  assert.equal(pauses, 2);
});

test("Arker-only creation restrictions reject unsupported options before any request", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG", "ARKER_SOURCE_VM_ID");
  process.env.ARKER_API_KEY = "arker-test";
  process.env.ARKER_SOURCE_VM_ID = "00000000000000000000000000";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  for (const options of [{ headers: {} }, { proxy: "unsupported" }, { network: { maskRequestHost: true } }]) {
    await assert.rejects(Sandbox.create(options as any), /unsupported .*option/i);
  }
});

test("Arker create timeout bounds use native 400 errors before creating a VM", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG", "ARKER_SOURCE_VM_ID");
  process.env.ARKER_API_KEY = "arker-test";
  process.env.ARKER_SOURCE_VM_ID = "00000000000000000000000000";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  for (const timeoutMs of [-1000, 0, 86_401_000]) {
    await assert.rejects(Sandbox.create({ timeoutMs }), error => error instanceof SandboxError
      && error.constructor === SandboxError && error.statusCode === 400);
  }
});

test("traffic token property preserves native optional values and reads VM-bound state", () => {
  for (const token of [undefined, "synthetic-traffic-token"]) {
    const native = Reflect.construct(Sandbox, [{ provider: "e2b", sandboxId: "native" }, { trafficAccessToken: token }]) as Sandbox;
    assert.equal(native.trafficAccessToken, token);
  }
  const vm = {} as VM;
  const sandbox = Reflect.construct(Sandbox, [{ provider: "arker", sandboxId: "fixture", getInstance: () => vm }]) as Sandbox;
  assert.equal(sandbox.trafficAccessToken, undefined);
  rememberTrafficToken(vm, "synthetic-app-token");
  assert.equal(sandbox.trafficAccessToken, "synthetic-app-token");
  rememberTrafficToken(vm, null);
  assert.equal(sandbox.trafficAccessToken, undefined);
});

test("provider fork hook installs policies in the only fork request", async t => {
  preserveProperties(t, Arker.prototype, "fork");
  const calls: unknown[] = [];
  Arker.prototype.fork = async function(request) {
    calls.push(request);
    return { id: "fixture" } as VM;
  };
  const policy = { rules: { app: { type: "inbound" as const, priority: 0, action: "allow" as const, auth: "open" as const,
    match: { ports: [8080], headers: { "e2b-traffic-access-token": ["${secret:APP}"] } } } }, secrets: { APP: "synthetic-app-token" } };
  const provider = createArkerComputeProvider({ apiKey: "synthetic-key", sourceVmId: "fixture-source" }, undefined, undefined,
    async (client, request) => {
      assert.equal(calls.length, 0);
      assert.equal(request.source_vm_id, "fixture-source");
      return client.fork({ ...request, policies: policy });
    });
  await provider.sandbox.create();
  assert.deepEqual(calls, [{ source_vm_id: "fixture-source", policies: policy }]);
});

test("Arker creation separates native credentials and keeps default kill semantics", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG", "ARKER_SOURCE_VM_ID");
  process.env.ARKER_API_KEY = "arker-only";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  delete process.env.ARKER_SOURCE_VM_ID;
  preserveProperties(t, Arker.prototype, "getVm", "fork");
  const requests: unknown[] = [];
  const marker = new Error("stop at fork");
  Arker.prototype.getVm = (async function (this: Arker, source: string) {
    assert.equal(source, "selected-template");
    assert.equal(this._authHeaders().authorization, "Bearer arker-only");
    return { policies: undefined } as VM;
  }) as typeof Arker.prototype.getVm;
  Arker.prototype.fork = async function (request) {
    assert.equal(this._authHeaders().authorization, "Bearer arker-only");
    requests.push(request);
    throw marker;
  };
  for (const lifecycle of [undefined, {}, { autoResume: false }, { onTimeout: "kill" }, { onTimeout: { action: "kill" }, autoResume: false }]) {
    const options = { template: "selected-template", apiKey: "native-only", secure: false, validateApiKey: false, debug: false, lifecycle };
    const original = structuredClone(options);
    await assert.rejects(Sandbox.create(options as any), error => error === marker);
    const request = requests.at(-1) as any;
    assert.equal(request.source_vm_name, "selected-template");
    assert.deepEqual(request.policies.rules["computesdk-e2b-lifetime"], { type: "schedule", after_created: 300, action: "delete" });
    assert.equal(request.policies.rules["computesdk-e2b-envd"].auth, "arker");
    assert.ok(!JSON.stringify(request).includes("native-only"));
    assert.deepEqual(options, original);
  }
});

test("unsupported creation behaviors fail before source lookup or fork", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "arker-only";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  preserveProperties(t, Arker.prototype, "getVm", "fork");
  Arker.prototype.getVm = async () => { assert.fail("must not look up source"); };
  Arker.prototype.fork = async () => { assert.fail("must not fork"); };
  for (const options of [
    { debug: true }, { lifecycle: { onTimeout: "pause" } },
    { lifecycle: { onTimeout: { action: "pause", mode: "filesystem" } } },
    { lifecycle: { autoResume: true } }, { lifecycle: { autoResume: 0 } },
    { lifecycle: { onTimeout: { action: "kill", mode: "full" } } },
    { lifecycle: { unknown: false } },
  ]) await assert.rejects(Sandbox.create(options as any), /supports only|unsupported option/);
});

test("native object template and credentials remain native without Arker authentication", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  preserveProperties(t, Native, "create");
  preserveProperties(t, Arker.prototype, "fork");
  Arker.prototype.fork = async () => { assert.fail("native key must not enable Arker"); };
  const options = { template: "native-template", apiKey: "native-only", secure: false, validateApiKey: true, debug: false,
    lifecycle: { onTimeout: "pause" as const, autoResume: true }, requestTimeoutMs: 0 };
  Native.create = (async (actual: unknown) => {
    assert.deepEqual(actual, options);
    return { sandboxId: "native" } as Native;
  }) as typeof Native.create;
  assert.equal((await Sandbox.create(options)).sandboxId, "native");
});

test("cancelled Arker creation deletes the known VM with an uncancelled cleanup request", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_BASE_URL", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "arker-only";
  process.env.ARKER_BASE_URL = "https://example.invalid";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  preserveProperties(t, Arker.prototype, "getVm", "fork", "_request");
  const factory = Sandbox as unknown as { dataPlane: (box: { getInstance(): VM }) => Promise<unknown> };
  preserveProperties(t, factory, "dataPlane");
  const calls: string[] = [];
  Arker.prototype.getVm = (async () => ({ policies: undefined })) as unknown as typeof Arker.prototype.getVm;
  Arker.prototype.fork = async function () { calls.push("fork"); return this.vm("child"); };
  Arker.prototype._request = async function <T>(method: any, path: string, _body?: unknown, _base?: string, _headers?: unknown, _queue?: unknown, _retry?: unknown, transport?: { signal?: AbortSignal }): Promise<T> {
    transport?.signal?.throwIfAborted();
    assert.equal(method, "DELETE");
    assert.equal(path, "/v1/vms/child");
    calls.push("delete");
    return { deleted: true } as T;
  };
  const controller = new AbortController();
  const cancellation = new Error("caller cancelled");
  factory.dataPlane = async box => {
    controller.abort(cancellation);
    await box.getInstance()._client._request("POST", "/prepare");
    assert.fail("cancelled preparation must fail");
  };
  await assert.rejects(Sandbox.create("source", { requestTimeoutMs: 0, signal: controller.signal }), error => error === cancellation);
  assert.deepEqual(calls, ["fork", "delete"]);
});


test("late creation cancellation still deletes when native component close fails", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_BASE_URL", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "arker-only";
  process.env.ARKER_BASE_URL = "https://example.invalid";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  preserveProperties(t, Arker.prototype, "getVm", "fork", "_request");
  const factory = Sandbox as unknown as { dataPlane: () => Promise<unknown> };
  preserveProperties(t, factory, "dataPlane");
  Arker.prototype.getVm = (async () => ({ policies: undefined })) as unknown as typeof Arker.prototype.getVm;
  Arker.prototype.fork = async function () { return this.vm("child"); };
  let deleted = 0;
  Arker.prototype._request = async function <T>(method: any): Promise<T> {
    if (method === "DELETE") { deleted++; return { deleted: true } as T; }
    throw new Error("no hostname");
  };
  const controller = new AbortController();
  const cancellation = new Error("caller cancelled");
  const closeError = new Error("close failed");
  factory.dataPlane = async () => {
    controller.abort(cancellation);
    return { close() { throw closeError; } };
  };
  await assert.rejects(Sandbox.create("source", { requestTimeoutMs: 0, signal: controller.signal }), error => {
    assert.ok(error instanceof AggregateError);
    assert.deepEqual(error.errors, [cancellation, closeError]);
    return true;
  });
  assert.equal(deleted, 1);
});

for (const cancelled of [false, true]) test(`keyed replay preparation failure preserves sandbox ownership (cancelled=${cancelled})`, async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_BASE_URL", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "test-only-creator";
  process.env.ARKER_BASE_URL = "https://example.invalid";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  preserveProperties(t, Arker.prototype, "getVm", "fork", "_request");
  const factory = Sandbox as unknown as { dataPlane: (box: { getInstance(): VM }) => Promise<unknown> };
  preserveProperties(t, factory, "dataPlane");
  const requests: unknown[] = [];
  Arker.prototype.getVm = (async () => ({ policies: undefined })) as unknown as typeof Arker.prototype.getVm;
  Arker.prototype.fork = async function (request) { requests.push(structuredClone(request)); return this.vm("existing-child"); };
  Arker.prototype._request = async () => assert.fail("failed keyed retry must not delete existing child");
  const failure = new Error("preparation failed");
  let controller: AbortController;
  factory.dataPlane = async () => {
    if (cancelled) controller.abort(failure);
    throw failure;
  };
  for (const value of ["one", "one", "two"]) {
    controller = new AbortController();
    await assert.rejects(Sandbox.create("template", {
      idempotencyKey: "a".repeat(255), envs: { VALUE: value }, network: { allowPublicTraffic: false },
      signal: controller.signal,
    }), error => error === failure);
  }
  assert.deepEqual(requests[0], requests[1]);
  const [first, , changed] = requests as any[];
  assert.equal(first.idempotencyKey.length, 64);
  assert.equal(first.idempotencyKey, changed.idempotencyKey);
  assert.notDeepEqual(first.metadata, changed.metadata);
  assert.ok(!JSON.stringify(requests).includes("test-only-creator"));
});

test("public keyed facade replays one sandbox and translates only idempotency conflicts", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_BASE_URL", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "test-only-creator";
  process.env.ARKER_BASE_URL = "https://example.invalid";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  preserveProperties(t, Arker.prototype, "getVm", "fork", "_request");
  const factory = Sandbox as unknown as { dataPlane: () => Promise<unknown> };
  preserveProperties(t, factory, "dataPlane");
  factory.dataPlane = async () => ({ close() {} });
  Arker.prototype.getVm = (async () => ({ policies: undefined })) as unknown as typeof Arker.prototype.getVm;
  Arker.prototype._request = async () => { throw new Error("no hostname"); };
  const claims = new Map<string, { payload: string; vm: VM }>();
  Arker.prototype.fork = async function(request) {
    const key = request.idempotencyKey!;
    const payload = JSON.stringify(request);
    const previous = claims.get(key);
    if (previous) {
      if (previous.payload !== payload) throw new ArkerError("idempotency_conflict", "different request", 409);
      return previous.vm;
    }
    const vm = this.vm("existing-child");
    claims.set(key, {payload, vm});
    return vm;
  };
  const create = (value: string) => Sandbox.create("template", { idempotencyKey: "repeat-key", envs: { VALUE: value }, network: { allowPublicTraffic: false } });
  const first = await create("one"), second = await create("one");
  assert.equal(first.sandboxId, second.sandboxId);
  assert.equal(claims.size, 1);
  await assert.rejects(create("two"), error => error instanceof SandboxError && error.statusCode === 409);
});
