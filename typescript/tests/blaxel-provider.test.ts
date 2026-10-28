import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { createServer } from "node:http";
import { connect } from "node:net";
import { SandboxInstance as Native, settings } from "@blaxel/core";
import { SandboxInstance as Shim } from "../src/blaxel.js";
import { createNativeBlaxelProvider } from "../src/compat/blaxel-provider.js";
import { GuestProcesses } from "../src/compat/guest-process.js";
import {
  preserveProperties,
  preserveEnvironment,
  listen,
  closeServer,
} from "./provider-fixtures.js";

function nativeProvider(workspace: string) {
  return createNativeBlaxelProvider({ apiKey: "bl_test", workspace });
}

function stubNativeServer(t: TestContext, port: number) {
  preserveProperties(t, Native, "create");
  preserveProperties(t, settings.config, "disableH2");
  settings.config.disableH2 = true;
  Native.create = async () =>
    new Native({
      metadata: { name: "sandbox", url: `http://127.0.0.1:${port}` },
      spec: { runtime: { image: "test", memory: 512 } },
    });
}

test("Arker Blaxel process converts execution timeout seconds to internal milliseconds", async (t) => {
  const original = GuestProcesses.prototype.exec;
  preserveProperties(t, GuestProcesses.prototype, "exec");
  const captured: unknown[] = [];
  GuestProcesses.prototype.exec = async function (request) {
    captured.push(request.timeout);
    return {} as Awaited<ReturnType<typeof original>>;
  };
  const box = {
    provider: "arker",
    sandboxId: "vm-test",
    getInstance: () => ({}),
  };
  const sandbox = Reflect.construct(Shim, [box]) as Shim;
  for (const timeout of [1, 0, undefined]) {
    await sandbox.process.exec({
      command: "true",
      ...(timeout === undefined ? {} : { timeout }),
    });
  }
  assert.deepEqual(captured, [1000, 0, undefined]);
});

test("native Blaxel adapter converts ComputeSDK command milliseconds to seconds", async (t) => {
  const captured: unknown[] = [];
  const instance = {
    metadata: { name: "timeout-test" },
    process: {
      exec: async (request: { timeout?: number }) => {
        captured.push(request.timeout);
        return { stdout: "", stderr: "", exitCode: 0 };
      },
    },
  } as unknown as Native;
  const provider = nativeProvider("test");
  preserveProperties(t, Native, "get");
  Native.get = async () => instance;
  const sandbox = await provider.instances.getById("timeout-test");
  assert(sandbox);
  for (const timeout of [1000, 0, undefined]) {
    await sandbox.runCommand(
      "true",
      timeout === undefined ? {} : { timeout },
    );
  }
  assert.deepEqual(captured, [1, 0, undefined]);
});

test("Arker Blaxel fetch preserves regionless and creation-time transport selection", async (t) => {
  preserveProperties(t, settings.config, "disableH2");
  preserveEnvironment(t, "BL_REGION");
  delete process.env.BL_REGION;
  const server = createServer((_request, response) => {
    response.writeHead(200, { "content-length": "0" });
    response.end();
  });
  const address = await listen(server);
  const vm = { connectPort: async () => connect(address.port, "127.0.0.1") };
  const box = {
    provider: "arker",
    sandboxId: "vm-test",
    getInstance: () => vm,
  };
  try {
    for (const [region, initiallyDisabled, finallyDisabled, hasBody] of [
      [undefined, false, false, false],
      ["us-pdx-1", false, false, true],
      ["us-pdx-1", false, true, false],
      ["us-pdx-1", true, false, false],
    ] as const) {
      settings.config.disableH2 = initiallyDisabled;
      const sandbox = Reflect.construct(Shim, [
        box,
        undefined,
        { region, disableH2: initiallyDisabled },
      ]) as Shim;
      settings.config.disableH2 = finallyDisabled;
      const response = await sandbox.fetch(8080, "/head", { method: "HEAD" });
      assert.equal(
        response.body !== null,
        hasBody,
        `region=${region}, initial H2 disabled=${initiallyDisabled}, current=${finallyDisabled}`,
      );
      assert.equal((await response.arrayBuffer()).byteLength, 0);
    }
  } finally {
    await closeServer(server);
  }
});

test("in-shim Blaxel creation preserves names, credentials, network inputs and strict/get-or-create flags", async (t) => {
  preserveProperties(t, Native, "create", "get", "delete");
  const calls: unknown[] = [];
  const native = {
    metadata: { name: "requested-name", createdAt: "2026-10-17T00:00:00Z" },
  } as Native;
  Native.create = async (options, callOptions) => {
    calls.push([options, callOptions]);
    return native;
  };
  Native.get = async () => native;
  Native.delete = async (id) => {
    calls.push(["delete", id]);
    return {
      metadata: native.metadata,
      spec: { runtime: { image: "test", memory: 512 } },
    };
  };
  const provider = nativeProvider("test-workspace");
  const network = { enable: false };
  const sandbox = await provider.instances.create({
    name: "requested-name",
    envs: { VALUE: "kept" },
    network,
    timeout: 300_000,
    safe: false,
    createIfNotExist: false,
    creationTimeout: 17,
  });
  assert.equal(sandbox.sandboxId, "requested-name");
  assert.equal(sandbox.getInstance().metadata, native.metadata);
  assert.deepEqual(calls[0], [
    {
      name: "requested-name",
      envs: [{ name: "VALUE", value: "kept" }],
      network,
      ttl: "300s",
    },
    { createIfNotExist: false, safe: false, timeout: 17 },
  ]);
  await provider.instances.create({
    name: "requested-name",
    createIfNotExist: true,
  });
  assert.deepEqual(calls[1], [
    { name: "requested-name" },
    { createIfNotExist: true },
  ]);
  const denied = Object.assign(new Error("forbidden"), { status: 403 });
  for (const [method, operation] of [
    ["get", () => provider.instances.getById("requested-name")],
    ["delete", () => provider.instances.destroy("requested-name")],
    ["create", () => provider.instances.create()],
  ] as const) {
    Native[method] = async () => {
      throw denied;
    };
    await assert.rejects(operation(), (error) => error === denied);
  }
  Native.get = async () => {
    throw Object.assign(new Error("opaque"), { status: 404 });
  };
  assert.equal(await provider.instances.getById("missing"), null);
});

test("native async actions re-enter their originating workspace epoch", async (t) => {
  preserveProperties(t, Native, "create");
  const observations: string[] = [];
  Native.create = async () =>
    ({
      metadata: { name: "sandbox" },
      process: {
        async get() {
          const workspace = settings.workspace;
          await new Promise((resolve) => setTimeout(resolve, 5));
          assert.equal(settings.workspace, workspace);
          observations.push(workspace);
          return { workspace };
        },
      },
    }) as unknown as Native;
  const first = await nativeProvider("first").instances.create();
  const second = await nativeProvider("second").instances.create();
  await Promise.all([
    first.getInstance().process.get("process"),
    second.getInstance().process.get("process"),
  ]);
  assert.deepEqual(observations, ["first", "second"]);
});

test("native synchronous watchers own request and content-read credential epochs without pinning other workspaces", async (t) => {
  preserveProperties(t, Native, "create");
  const observations: string[] = [];
  let completeRequest!: () => void;
  const requestPending = new Promise<void>((resolve) => {
    completeRequest = resolve;
  });
  let requestStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    requestStarted = resolve;
  });
  let contentRead!: () => void;
  const content = new Promise<void>((resolve) => {
    contentRead = resolve;
  });
  let closed = false;
  Native.create = async () =>
    ({
      metadata: { name: "sandbox" },
      fs: {
        get url() {
          return settings.workspace;
        },
        withClient(options: object) {
          return {
            ...options,
            client: {
              async get() {
                requestStarted();
                await requestPending;
                observations.push(`request:${settings.workspace}`);
              },
            },
          };
        },
        async read() {
          observations.push(`read:${settings.workspace}`);
          contentRead();
        },
        watch(_path: string, callback: () => void) {
          observations.push(`watch:${this.url}`);
          const options = this.withClient({});
          void options.client.get().then(async () => {
            await new Promise<void>((resolve) => setImmediate(resolve));
            if (!closed) {
              await this.read();
              callback();
            }
          });
          return {
            close() {
              closed = true;
            },
          };
        },
      },
    }) as unknown as Native;
  const first = await nativeProvider("first").instances.create();
  await nativeProvider("second").instances.create();
  const watcher = first.getInstance().fs.watch("/blaxel", () => {});
  await started;
  const second = nativeProvider("second").instances.create();
  completeRequest();
  await second;
  await content;
  watcher.close();
  assert.deepEqual(observations, [
    "watch:first",
    "request:first",
    "read:first",
  ]);
  assert.equal(closed, true);
});

test("closing an owned watcher immediately prevents its queued request", async (t) => {
  preserveProperties(t, Native, "create");
  let starts = 0;
  Native.create = async () =>
    ({
      metadata: { name: "sandbox" },
      fs: {
        watch() {
          starts++;
          return { close() {} };
        },
      },
    }) as unknown as Native;
  const sandbox = await nativeProvider("first").instances.create();
  sandbox
    .getInstance()
    .fs.watch("/blaxel", () => {})
    .close();
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(starts, 0);
});

test(
  "real native watch HTTP headers and withContent reads stay in their owning workspace",
  { timeout: 10_000 },
  async (t) => {
    const observations: string[] = [];
    let stream!: import("node:http").ServerResponse;
    let started!: () => void;
    const ready = new Promise<void>((resolve) => {
      started = resolve;
    });
    let received!: (event: unknown) => void;
    const event = new Promise<unknown>((resolve) => {
      received = resolve;
    });
    const server = createServer((request, response) => {
      assert(!request.url?.includes("Promise"));
      assert(request.url?.includes("blaxel"));
      observations.push(
        `${request.url?.startsWith("/watch/") ? "watch" : "read"}:${request.headers["x-blaxel-workspace"]}`,
      );
      if (request.url?.startsWith("/watch/")) {
        stream = response;
        response.writeHead(200, { "content-type": "application/x-ndjson" });
        response.flushHeaders();
        started();
      } else {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({ content: "owned-content" }));
      }
    });
    const address = await listen(server);
    stubNativeServer(t, address.port);
    let watcher: { close(): void } | undefined;
    try {
      const first = await nativeProvider("first").instances.create();
      await nativeProvider("second").instances.create();
      watcher = first.getInstance().fs.watch("/blaxel", received, {
        withContent: true,
        onError(error) {
          throw error;
        },
      });
      await ready;
      await nativeProvider("second").instances.create();
      stream.write(
        `${JSON.stringify({ op: "WRITE", path: "/blaxel", name: "file" })}\n`,
      );
      assert.deepEqual(await event, {
        op: "WRITE",
        path: "/blaxel",
        name: "file",
        content: "owned-content",
      });
      assert.deepEqual(observations, ["watch:first", "read:first"]);
    } finally {
      watcher?.close();
      await closeServer(server);
    }
  },
);

test("credential epochs preserve the application's disabled-H2 transport setting", async (t) => {
  preserveProperties(t, Native, "create");
  preserveProperties(t, settings.config, "disableH2");
  settings.config.disableH2 = true;
  Native.create = async () => {
    assert.equal(settings.disableH2, true);
    return { metadata: { name: "transport" } } as Native;
  };
  await nativeProvider("test").instances.create();
  assert.equal(settings.disableH2, true);
});

test("createIfNotExists delegates reconciliation and options to create without a lookup", async (t) => {
  preserveProperties(t, Shim, "create", "get");
  const calls: unknown[] = [];
  const sandbox = {} as Shim;
  Shim.create = async (...args) => { calls.push(args); return sandbox; };
  Shim.get = async () => { throw new Error("unexpected preliminary lookup"); };
  for (const options of [{ name: "existing" }, {}]) {
    assert.equal(await Shim.createIfNotExists(options, { timeout: 17, safe: false }), sandbox);
    assert.deepEqual(calls.at(-1), [options, { timeout: 17, safe: false, createIfNotExist: true }]);
  }
  const conflict = new Error("conflict");
  Shim.create = async () => { throw conflict; };
  await assert.rejects(Shim.createIfNotExists({ name: "existing" }), error => error === conflict);
});

test("native log streams preserve workspace, callbacks, wait errors and cancellation without blocking other workspaces", { timeout: 10_000 }, async (t) => {
  const observations: string[] = [];
  let response!: import("node:http").ServerResponse;
  let started!: () => void;
  const ready = new Promise<void>(resolve => { started = resolve; });
  const server = createServer((request, reply) => {
    observations.push(String(request.headers["x-blaxel-workspace"]));
    if (request.url?.includes("failure")) {
      reply.writeHead(403, { "content-type": "application/json" });
      reply.end(JSON.stringify({ error: "denied" }));
      return;
    }
    response = reply;
    reply.writeHead(200, { "content-type": "text/plain" });
    reply.flushHeaders();
    started();
  });
  const address = await listen(server);
  stubNativeServer(t, address.port);
  let stream: ReturnType<Native["process"]["streamLogs"]> | undefined;
  try {
    const first = await nativeProvider("first").instances.create();
    const second = nativeProvider("second");
    await second.instances.create();
    const stdout: string[] = [], stderr: string[] = [], logs: string[] = [];
    stream = first.getInstance().process.streamLogs("process", {
      onStdout: value => stdout.push(value), onStderr: value => stderr.push(value), onLog: value => logs.push(value),
    });
    await ready;
    await second.instances.create();
    response.write("stdout:hel");
    response.end("lo\nstderr:problem\n[keepalive]\nstdout:tail");
    await stream.wait();
    assert.deepEqual(stdout, ["hello", "tail"]);
    assert.deepEqual(stderr, ["problem"]);
    assert.deepEqual(logs, ["hello", "problem", "tail"]);
    const errors: Error[] = [];
    await assert.rejects(first.getInstance().process.streamLogs("failure", { onError: error => errors.push(error) }).wait(), /403/);
    assert.equal(errors.length, 1);
    stream = first.getInstance().process.streamLogs("cancelled");
    stream.close();
    await stream.wait();
    assert.deepEqual(observations, ["first", "first"]);
  } finally {
    stream?.close();
    await closeServer(server);
  }
});
