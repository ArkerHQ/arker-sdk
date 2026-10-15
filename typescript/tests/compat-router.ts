import assert from "node:assert/strict";

import { Blaxel } from "../src/blaxel.js";
import { Daytona } from "../src/daytona.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { ModalClient } from "../src/modal.js";
import { setCompatComputeFactoryForTests } from "../src/compat/runtime.js";

type CapturedConfig = {
  providers: Array<{ name?: string }>;
  routerConfigPath?: string;
};

function installRouterProbe() {
  const captured: CapturedConfig[] = [];
  const ids = new Map([
    ["e2b-id", "e2b"],
    ["modal-id", "modal"],
    ["daytona-id", "daytona"],
    ["blaxel-id", "blaxel"],
  ]);
  setCompatComputeFactoryForTests((config: unknown) => {
    captured.push(config as CapturedConfig);
    return {
      instances: {
        create: async () => fakeInstance("new-id", "arker"),
        getById: async (id: string) => ids.has(id) ? fakeInstance(id, ids.get(id)!) : null,
      },
      sandbox: {
        create: async () => fakeInstance("new-id", "arker"),
        getById: async (id: string) => ids.has(id) ? fakeInstance(id, ids.get(id)!) : null,
      },
    } as any;
  });
  return { captured, cleanup: () => setCompatComputeFactoryForTests() };
}

function fakeInstance(id: string, provider: string) {
  return {
    instanceId: id,
    sandboxId: id,
    provider,
    runCommand: async (command: string, options?: unknown) => ({
      stdout: `${provider}:${command}:${JSON.stringify(options ?? {})}`,
      stderr: "",
      exitCode: 0,
      durationMs: 1,
    }),
    startProcess: async () => ({ jobId: "job", pid: 1 }),
    filesystem: {
      readFile: async () => "",
      writeFile: async () => undefined,
      mkdir: async () => undefined,
      readdir: async () => [],
      exists: async () => true,
      remove: async () => undefined,
    },
    destroy: async () => undefined,
  };
}

async function testEveryShimUsesTheSameRouterFile(): Promise<void> {
  const probe = installRouterProbe();
  try {
    await E2BSandbox.create(undefined, { routerConfigPath: "/config/computesdk_router.json" });
    new ModalClient({ routerConfigPath: "/config/computesdk_router.json" });
    new Daytona({ routerConfigPath: "/config/computesdk_router.json" });
    await new Blaxel({ routerConfigPath: "/config/computesdk_router.json" }).get("blaxel-id");

    assert.equal(probe.captured.length, 4);
    for (const config of probe.captured) {
      assert.equal(config.routerConfigPath, "/config/computesdk_router.json");
      assert.equal(config.providers[0]?.name, "arker");
    }
    assert.deepEqual(probe.captured.map((config) => config.providers[1]?.name), ["e2b", "modal", "daytona", "blaxel"]);
  } finally {
    probe.cleanup();
  }
}

async function testExistingIdsCanResolveToEveryNativeProvider(): Promise<void> {
  const probe = installRouterProbe();
  try {
    assert.equal((await E2BSandbox.connect("e2b-id")).provider, "e2b");
    assert.equal((await new ModalClient().sandboxes.fromId("modal-id")).provider, "modal");
    assert.equal((await new Daytona().get("daytona-id")).provider, "daytona");
    assert.equal((await new Blaxel().get("blaxel-id")).provider, "blaxel");
  } finally {
    probe.cleanup();
  }
}

await testEveryShimUsesTheSameRouterFile();
await testExistingIdsCanResolveToEveryNativeProvider();

console.log("PASS compat router");
