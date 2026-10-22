import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Daytona as Native } from "@daytona/sdk";
import { Daytona as Shim } from "../src/daytona.js";
import { recordEvidence } from "../scripts/compat-evidence.js";

async function program(Client: typeof Native | typeof Shim) {
  const client = new Client();
  try {
    const sandbox = await client.create();
    try {
      const result = await sandbox.process.executeCommand("pwd");
      assert.equal(result.exitCode, 0);
      assert.equal(result.result.trim(), "/home/daytona");
      assert.equal(result.artifacts?.stdout, result.result);
      assert.equal(result.artifacts?.charts, undefined);
      const changed = await sandbox.process.executeCommand(
        'printf "%s:" "$VALUE"; pwd',
        "/tmp",
        { VALUE: "native" },
      );
      assert.deepEqual(
        [changed.exitCode, changed.result],
        [0, "native:/tmp\n"],
      );
      const connected = await client.get(sandbox.id);
      const isolated = await connected.process.executeCommand(
        'printf "%s:" "${VALUE-unset}"; pwd',
      );
      assert.deepEqual(
        [isolated.exitCode, isolated.result],
        [0, "unset:/home/daytona\n"],
      );
      const failed = await connected.process.executeCommand(
        "printf nonzero; exit 17",
      );
      assert.deepEqual([failed.exitCode, failed.result], [17, "nonzero"]);
    } finally {
      await client.delete(sandbox as any);
    }
  } finally {
    await client[Symbol.asyncDispose]();
  }
}

if (!process.env.DAYTONA_API_KEY || !process.env.ARKER_API_KEY)
  throw new Error("Live Daytona and Arker credentials required");
const directory = await mkdtemp(join(tmpdir(), "compat-daytona-import-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
const key = process.env.DAYTONA_API_KEY;
try {
  for (const [provider, Client, route] of [
    ["daytona", Native, "reference"],
    ["daytona", Shim, "native"],
    ["arker", Shim, "arker"],
  ] as const) {
    const path = join(directory, provider + ".json");
    await writeFile(
      path,
      JSON.stringify({
        version: 1,
        existing: { strategy: "priority", order: [provider] },
        create: { strategy: "priority", order: [provider] },
      }),
    );
    process.env.COMPUTESDK_ROUTER_CONFIG = path;
    if (provider === "arker") delete process.env.DAYTONA_API_KEY;
    else process.env.DAYTONA_API_KEY = key;
    let outcome = "failed";
    try {
      await program(Client);
      outcome = "passed";
      console.log(`PASS Daytona unchanged program: ${route}`);
    } finally {
      await recordEvidence(
        "daytona",
        route,
        "import-only-create-get-exec-delete",
        [
          "Daytona.create",
          "Daytona.get",
          "Daytona.delete",
          "Sandbox.id",
          "Sandbox.process.executeCommand",
        ],
        outcome,
      );
    }
  }
} finally {
  process.env.DAYTONA_API_KEY = key;
  if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
  else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
  await rm(directory, { recursive: true });
}
