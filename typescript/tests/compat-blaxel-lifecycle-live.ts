import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SandboxInstance as Native } from "@blaxel/core";
import { SandboxInstance as Shim } from "../src/blaxel.js";

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

if (!process.env.BL_API_KEY || !process.env.ARKER_API_KEY)
  throw new Error("Live Blaxel and Arker credentials required");
const directory = await mkdtemp(join(tmpdir(), "blaxel-lifecycle-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
let reference: Awaited<ReturnType<typeof program>> | undefined;
try {
  for (const [provider, constructor] of [
    ["blaxel", Native],
    ["blaxel", Shim],
    ["arker", Shim],
  ] as const) {
    const path = join(directory, "router.json");
    await writeFile(
      path,
      JSON.stringify({
        version: 1,
        existing: { strategy: "priority", order: [provider] },
        create: { strategy: "priority", order: [provider] },
      }),
    );
    process.env.COMPUTESDK_ROUTER_CONFIG = path;
    const result = await program(constructor);
    if (constructor === Native) reference = result;
    else assert.deepEqual(result, reference);
    console.log(
      `PASS named strict creation: ${constructor === Native ? "reference" : "shim"} on ${provider}; code=${result.code}`,
    );
  }
} finally {
  if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
  else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
  await rm(directory, { recursive: true });
}
