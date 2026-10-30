import assert from "node:assert/strict";
import { Daytona as Native } from "@daytona/sdk";
import { Daytona as Shim } from "../src/daytona.js";
import { Cleanup, recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";

async function program(Client: typeof Native | typeof Shim, cleanup: Cleanup) {
  const client = new Client();
  try {
    const sandbox = await client.create();
    cleanup.allocated(sandbox.id);
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
      await cleanup.observeAbsent(sandbox.id, id => client.get(id));
    }
  } finally {
    await client[Symbol.asyncDispose]();
  }
}

await withLiveRouter(["DAYTONA_API_KEY", "ARKER_API_KEY"], async ({ setProvider }) => {
  const key = process.env.DAYTONA_API_KEY;
  for (const [provider, Client, route] of [
    ["daytona", Native, "reference"],
    ["daytona", Shim, "native"],
    ["arker", Shim, "arker"],
  ] as const) {
    await setProvider(provider);
    if (provider === "arker") delete process.env.DAYTONA_API_KEY;
    else process.env.DAYTONA_API_KEY = key;
    const cleanup = new Cleanup("daytona");
    let outcome = "failed";
    try {
      await program(Client, cleanup);
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
        outcome, {}, cleanup.receipt(),
      );
    }
  }
});
