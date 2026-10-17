import assert from "node:assert/strict";
import { test } from "node:test";
import { SandboxInstance as Native, settings } from "@blaxel/core";
import { createNativeBlaxelProvider } from "../src/compat/blaxel-provider.js";

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
