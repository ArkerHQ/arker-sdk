import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { Arker, RouterLookupTimeoutError } from "@arker-ai/sdk";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";

const require = createRequire(import.meta.url);
const provider = process.argv[2];
const vendors = {
  e2b: "e2b",
  blaxel: "@blaxel/core",
  daytona: "@daytona/sdk",
  modal: "modal",
};
for (const name of ["e2b", "blaxel", "daytona", "modal"]) {
  assert.throws(() => require.resolve(`@computesdk/${name}`), {
    code: "MODULE_NOT_FOUND",
  });
  if (name !== provider)
    assert.throws(() => require.resolve(vendors[name]), {
      code: "MODULE_NOT_FOUND",
    });
}
assert.equal(typeof Arker, "function");
assert.equal(typeof require("@arker-ai/sdk").Arker, "function");
const CommonTimeout = require("@arker-ai/sdk").RouterLookupTimeoutError;
assert.ok(new RouterLookupTimeoutError("test") instanceof CommonTimeout);
assert.ok(new CommonTimeout("test") instanceof RouterLookupTimeoutError);
delete process.env.ARKER_API_KEY;
delete process.env.COMPUTESDK_ROUTER_CONFIG;

async function verifyE2B(native, shim) {
  const previous = native.Sandbox.create;
  const failure = new native.CommandExitError({
    stdout: "out",
    stderr: "err",
    exitCode: 17,
    error: "exit status 17",
  });
  native.Sandbox.create = async () => ({
    sandboxId: "consumer-e2b",
    commands: {
      run: async () => {
        throw failure;
      },
    },
  });
  try {
    const sandbox = await shim.Sandbox.create();
    await assert.rejects(
      sandbox.commands.run("exit 17"),
      (error) => error === failure,
    );
  } finally {
    native.Sandbox.create = previous;
  }
  const directory = await mkdtemp(join(tmpdir(), "arker-packed-timeout-"));
  const path = join(directory, "router.json");
  await writeFile(path, JSON.stringify({
    version: 1, existing: { strategy: "priority", order: ["e2b"], timeoutMs: 10 },
    create: { strategy: "priority", order: ["e2b"] },
  }));
  const previousGetInfo = native.Sandbox.getInfo;
  let release;
  native.Sandbox.getInfo = () => new Promise((resolve) => { release = resolve; });
  try {
    await assert.rejects(shim.Sandbox.connect("consumer-timeout", { routerConfigPath: path }), (error) => (
      error instanceof RouterLookupTimeoutError && error instanceof CommonTimeout
    ));
  } finally {
    release?.(null);
    native.Sandbox.getInfo = previousGetInfo;
    await rm(directory, { recursive: true });
  }
}

async function verifyBlaxel(native, shim) {
  const previous = native.SandboxInstance.create;
  let received;
  native.SandboxInstance.create = async (options, flags) => {
    received = { options, flags };
    return {
      metadata: { name: options.name },
      process: {
        exec: async () => ({ stdout: "consumer", stderr: "", exitCode: 0 }),
      },
    };
  };
  try {
    const sandbox = await shim.SandboxInstance.create({
      name: "consumer-blaxel",
    });
    assert.equal(sandbox.metadata.name, "consumer-blaxel");
    assert.equal(received.flags.createIfNotExist, false);
    assert.equal(
      (
        await sandbox.process.exec({
          command: "printf consumer",
          waitForCompletion: true,
        })
      ).stdout,
      "consumer",
    );
  } finally {
    native.SandboxInstance.create = previous;
  }
}

async function verifyDaytona(native, shim) {
  const previous = native.Daytona.prototype.create;
  const image = native.Image.base("alpine:3.21").runCommands("echo marker");
  const response = {
    exitCode: 17,
    result: "marker",
    artifacts: { stdout: "marker" },
  };
  native.Daytona.prototype.create = async (params) => {
    assert.equal(params.image, image);
    return {
      id: "consumer-daytona",
      process: { executeCommand: async () => response },
    };
  };
  const client = new shim.Daytona({ apiKey: "dtn-test" });
  try {
    const sandbox = await client.create({ image });
    assert.equal(await sandbox.process.executeCommand("exit 17"), response);
  } finally {
    native.Daytona.prototype.create = previous;
    await client[Symbol.asyncDispose]();
  }
}

async function verifyModal(native, shim) {
  const previous = native.SandboxService.prototype.create;
  const app = { appId: "ap-consumer" };
  const image = { imageId: "im-consumer" };
  const process = {
    stdout: { readText: async () => "consumer" },
    stderr: { readText: async () => "" },
    wait: async () => 17,
  };
  native.SandboxService.prototype.create = async (owner, source, options) => {
    assert.equal(owner, app);
    assert.equal(source, image);
    assert.deepEqual(options, { blockNetwork: true, encryptedPorts: [8443] });
    return { sandboxId: "sb-consumer", exec: async () => process };
  };
  const client = new shim.ModalClient({
    tokenId: "ak-test",
    tokenSecret: "as-test",
  });
  try {
    const sandbox = await client.sandboxes.create(app, image, {
      blockNetwork: true,
      encryptedPorts: [8443],
    });
    assert.equal(await sandbox.exec(["sh", "-c", "exit 17"]), process);
  } finally {
    native.SandboxService.prototype.create = previous;
    client.close();
  }
}

if (provider === "base") {
  console.log(
    "PASS packed SDK base consumer: ESM/CommonJS imports with no vendor SDKs installed",
  );
} else {
  const verify = {
    e2b: verifyE2B,
    blaxel: verifyBlaxel,
    daytona: verifyDaytona,
    modal: verifyModal,
  }[provider];
  assert.ok(verify, "Unknown consumer profile");
  await verify(
    await import(vendors[provider]),
    await import(`@arker-ai/sdk/${provider}`),
  );
  await verify(
    require(vendors[provider]),
    require(`@arker-ai/sdk/${provider}`),
  );
  console.log(
    `PASS packed SDK ${provider} consumer: ESM/CommonJS native identity and inputs; no unrelated SDKs or vendor adapters installed`,
  );
}
