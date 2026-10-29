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
import { Arker, type VM } from "../src/index.js";
import { rememberTrafficToken } from "../src/compat/e2b-network.js";
import { createArkerComputeProvider } from "../src/compat/arker-provider.js";
import { createNativeE2BProvider } from "../src/compat/e2b-provider.js";
import { preserveProperties, preserveEnvironment, listen } from "./provider-fixtures.js";

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
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
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

test("Arker-only creation restrictions reject vendor credentials before any request", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG", "ARKER_SOURCE_VM_ID");
  process.env.ARKER_API_KEY = "arker-test";
  process.env.ARKER_SOURCE_VM_ID = "00000000000000000000000000";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  for (const options of [{ apiKey: "native-test" }, { secure: true }, { network: { maskRequestHost: true } }]) {
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
