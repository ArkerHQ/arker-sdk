import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { Arker } from "@arker-ai/sdk";
import { Sandbox as NativeE2B, CommandExitError } from "e2b";
import { Sandbox as E2B } from "@arker-ai/sdk/e2b";
import { SandboxInstance as NativeBlaxel } from "@blaxel/core";
import { SandboxInstance as Blaxel } from "@arker-ai/sdk/blaxel";

const require = createRequire(import.meta.url);
for (const provider of ["@computesdk/e2b", "@computesdk/blaxel"])
  assert.throws(() => require.resolve(provider), { code: "MODULE_NOT_FOUND" });
assert.equal(typeof Arker, "function");
assert.equal(typeof require("@arker-ai/sdk").Arker, "function");
delete process.env.ARKER_API_KEY;
delete process.env.COMPUTESDK_ROUTER_CONFIG;

async function verify(
  NativeE2B,
  E2B,
  CommandExitError,
  NativeBlaxel,
  Blaxel,
  mode,
) {
  const failure = new CommandExitError({
    stdout: "out",
    stderr: "err",
    exitCode: 17,
    error: "exit status 17",
  });
  NativeE2B.create = async () => ({
    sandboxId: "consumer-e2b",
    commands: {
      run: async () => {
        throw failure;
      },
    },
  });
  const e2b = await E2B.create();
  await assert.rejects(
    e2b.commands.run("exit 17"),
    (error) => error === failure,
  );
  let nativeOptions;
  NativeBlaxel.create = async (options, flags) => {
    nativeOptions = { options, flags };
    return {
      metadata: { name: options.name },
      process: {
        exec: async () => ({ stdout: "consumer", stderr: "", exitCode: 0 }),
      },
    };
  };
  const blaxel = await Blaxel.create({ name: "consumer-blaxel" });
  assert.equal(blaxel.metadata.name, "consumer-blaxel");
  assert.equal(nativeOptions.flags.createIfNotExist, false);
  assert.equal(
    (
      await blaxel.process.exec({
        command: "printf consumer",
        waitForCompletion: true,
      })
    ).stdout,
    "consumer",
  );
  console.log(
    `PASS packed SDK ${mode} consumer: base/E2B/Blaxel imports, mocked native identity and creation inputs, no provider-package installs`,
  );
}
await verify(NativeE2B, E2B, CommandExitError, NativeBlaxel, Blaxel, "ESM");
const cjsE2B = require("e2b");
await verify(
  cjsE2B.Sandbox,
  require("@arker-ai/sdk/e2b").Sandbox,
  cjsE2B.CommandExitError,
  require("@blaxel/core").SandboxInstance,
  require("@arker-ai/sdk/blaxel").SandboxInstance,
  "CommonJS",
);
