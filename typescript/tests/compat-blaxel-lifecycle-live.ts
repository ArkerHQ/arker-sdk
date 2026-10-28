import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { SandboxInstance as Native } from "@blaxel/core";
import { SandboxInstance as Shim } from "../src/blaxel.js";
import { withLiveRouter } from "./compat-live-router.js";

async function program(SandboxInstance: typeof Native | typeof Shim) {
  const name = `arker-lifecycle-${randomUUID().slice(0, 12)}`;
  const sandbox = await SandboxInstance.create({ name });
  try {
    assert.equal(sandbox.metadata.name, name);
    assert.equal((await SandboxInstance.get(name)).metadata.name, name);
    let duplicate: Native | Shim | undefined;
    let conflict: unknown;
    try {
      duplicate = await SandboxInstance.create({ name });
    } catch (error) {
      conflict = error;
    } finally {
      if (duplicate) await duplicate.delete();
    }
    assert.ok(
      conflict,
      "Strict create returned a second sandbox instead of a conflict",
    );
    const error = conflict as {
      code?: unknown;
      status?: unknown;
      statusCode?: unknown;
      name?: unknown;
      constructor?: { name?: string };
    };
    assert.equal(
      typeof error.code,
      "string",
      "Conflict must preserve the vendor's structured code",
    );
    return {
      code: error.code,
      status: error.status ?? error.statusCode,
      name: error.name ?? error.constructor?.name,
    };
  } finally {
    await sandbox.delete();
  }
}

await withLiveRouter(["BL_API_KEY", "ARKER_API_KEY"], async ({ setProvider }) => {
  let reference: Awaited<ReturnType<typeof program>> | undefined;
  for (const [provider, constructor] of [
    ["blaxel", Native],
    ["blaxel", Shim],
    ["arker", Shim],
  ] as const) {
    await setProvider(provider);
    const result = await program(constructor);
    if (constructor === Native) reference = result;
    else assert.deepEqual(result, reference);
    console.log(
      `PASS named strict creation: ${constructor === Native ? "reference" : "shim"} on ${provider}; code=${result.code}`,
    );
  }
});
