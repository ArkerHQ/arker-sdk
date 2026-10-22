import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import { connect } from "node:net";
import { once } from "node:events";
import { SandboxInstance as Native, settings } from "@blaxel/core";
import { SandboxInstance as Shim } from "../src/blaxel.js";
import { createNativeBlaxelProvider } from "../src/compat/blaxel-provider.js";
import { GuestProcesses } from "../src/compat/guest-process.js";

test("Arker Blaxel process converts execution timeout seconds to internal milliseconds", async () => {
  const original = GuestProcesses.prototype.exec;
  const captured: unknown[] = [];
  GuestProcesses.prototype.exec = async function (request) {
    captured.push(request.timeout);
    return {} as Awaited<ReturnType<typeof original>>;
  };
  try {
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
  } finally {
    GuestProcesses.prototype.exec = original;
  }
});

test("native Blaxel adapter converts ComputeSDK command milliseconds to seconds", async () => {
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
  const provider = createNativeBlaxelProvider({
    apiKey: "bl_test",
    workspace: "test",
  });
  const original = Native.get;
  Native.get = async () => instance;
  try {
    const sandbox = await provider.instances.getById("timeout-test");
    assert(sandbox);
    for (const timeout of [1000, 0, undefined]) {
      await sandbox.runCommand(
        "true",
        timeout === undefined ? {} : { timeout },
      );
    }
    assert.deepEqual(captured, [1, 0, undefined]);
  } finally {
    Native.get = original;
  }
});

test("Arker Blaxel fetch preserves regionless and creation-time transport selection", async () => {
  const previousH2 = settings.config.disableH2;
  const previousEnvironment = process.env.BL_REGION;
  delete process.env.BL_REGION;
  const server = createServer((_request, response) => {
    response.writeHead(200, { "content-length": "0" });
    response.end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address !== "string");
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
    settings.config.disableH2 = previousH2;
    if (previousEnvironment === undefined) delete process.env.BL_REGION;
    else process.env.BL_REGION = previousEnvironment;
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test("in-shim Blaxel creation preserves names, credentials, network inputs and strict/get-or-create flags", async () => {
  const previous = {
    create: Native.create,
    get: Native.get,
    remove: Native.delete,
  };
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
  try {
    const provider = createNativeBlaxelProvider({
      apiKey: "bl_test",
      workspace: "test-workspace",
    });
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
    Native.get = async () => {
      throw denied;
    };
    await assert.rejects(
      provider.instances.getById("requested-name"),
      (error) => error === denied,
    );
    Native.get = async () => {
      throw Object.assign(new Error("opaque"), { status: 404 });
    };
    assert.equal(await provider.instances.getById("missing"), null);
    Native.delete = async () => {
      throw denied;
    };
    await assert.rejects(
      provider.instances.destroy("requested-name"),
      (error) => error === denied,
    );
    Native.create = async () => {
      throw denied;
    };
    await assert.rejects(
      provider.instances.create(),
      (error) => error === denied,
    );
  } finally {
    Native.create = previous.create;
    Native.get = previous.get;
    Native.delete = previous.remove;
  }
});

test("native async actions re-enter their originating workspace epoch", async () => {
  const previous = Native.create;
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
  try {
    const first = await createNativeBlaxelProvider({
      apiKey: "bl_test",
      workspace: "first",
    }).instances.create();
    const second = await createNativeBlaxelProvider({
      apiKey: "bl_test",
      workspace: "second",
    }).instances.create();
    await Promise.all([
      first.getInstance().process.get("process"),
      second.getInstance().process.get("process"),
    ]);
    assert.deepEqual(observations, ["first", "second"]);
  } finally {
    Native.create = previous;
  }
});

test("native synchronous watchers own request and content-read credential epochs without pinning other workspaces", async () => {
  const previous = Native.create;
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
  try {
    const first = await createNativeBlaxelProvider({
      apiKey: "bl_test",
      workspace: "first",
    }).instances.create();
    await createNativeBlaxelProvider({
      apiKey: "bl_test",
      workspace: "second",
    }).instances.create();
    const watcher = first.getInstance().fs.watch("/blaxel", () => {});
    await started;
    const second = createNativeBlaxelProvider({
      apiKey: "bl_test",
      workspace: "second",
    }).instances.create();
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
  } finally {
    Native.create = previous;
  }
});

test("closing an owned watcher immediately prevents its queued request", async () => {
  const previous = Native.create;
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
  try {
    const sandbox = await createNativeBlaxelProvider({
      apiKey: "bl_test",
      workspace: "first",
    }).instances.create();
    sandbox
      .getInstance()
      .fs.watch("/blaxel", () => {})
      .close();
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(starts, 0);
  } finally {
    Native.create = previous;
  }
});

test(
  "real native watch HTTP headers and withContent reads stay in their owning workspace",
  { timeout: 10_000 },
  async () => {
    const previous = {
      create: Native.create,
      disableH2: settings.config.disableH2,
    };
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
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    assert(address && typeof address !== "string");
    settings.config.disableH2 = true;
    Native.create = async () =>
      new Native({
        metadata: { name: "sandbox", url: `http://127.0.0.1:${address.port}` },
        spec: { runtime: { image: "test", memory: 512 } },
      });
    let watcher: { close(): void } | undefined;
    try {
      const first = await createNativeBlaxelProvider({
        apiKey: "bl_test",
        workspace: "first",
      }).instances.create();
      await createNativeBlaxelProvider({
        apiKey: "bl_test",
        workspace: "second",
      }).instances.create();
      watcher = first.getInstance().fs.watch("/blaxel", received, {
        withContent: true,
        onError(error) {
          throw error;
        },
      });
      await ready;
      await createNativeBlaxelProvider({
        apiKey: "bl_test",
        workspace: "second",
      }).instances.create();
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
      Native.create = previous.create;
      settings.config.disableH2 = previous.disableH2;
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      );
    }
  },
);

test("credential epochs preserve the application's disabled-H2 transport setting", async () => {
  const previous = {
    create: Native.create,
    disableH2: settings.config.disableH2,
  };
  settings.config.disableH2 = true;
  Native.create = async () => {
    assert.equal(settings.disableH2, true);
    return { metadata: { name: "transport" } } as Native;
  };
  try {
    await createNativeBlaxelProvider({
      apiKey: "bl_test",
      workspace: "test",
    }).instances.create();
    assert.equal(settings.disableH2, true);
  } finally {
    Native.create = previous.create;
    settings.config.disableH2 = previous.disableH2;
  }
});
