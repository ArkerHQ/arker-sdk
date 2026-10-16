import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { SandboxInstance as Native } from "@blaxel/core";
import { SandboxInstance as Shim } from "../src/blaxel.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { traceVendor } from "./compat-trace.js";

// This program uses only official Blaxel calls; the harness changes the import.
async function program(SandboxInstance: typeof Native | typeof Shim): Promise<void> {
  const sandbox = await SandboxInstance.create();
  try {
    assert.ok(Array.isArray((await sandbox.fs.ls("/blaxel")).files));
    const result = await sandbox.process.exec({
      command: "printf '%s:%s' \"$PWD\" \"$IMPORT_ONLY\"; printf diagnostic >&2",
      workingDir: "/tmp",
      env: { IMPORT_ONLY: "preserved" },
      waitForCompletion: true,
      timeout: 10_000,
    });
    assert.equal(result.stdout, "/tmp:preserved");
    assert.equal(result.stderr, "diagnostic");
    assert.equal(result.exitCode, 0);
    const isolated = await sandbox.process.exec({
      command: "printf '%s:%s' \"$PWD\" \"${IMPORT_ONLY-unset}\"",
      waitForCompletion: true,
    });
    assert.equal(isolated.stdout, "/blaxel:unset");
    await sandbox.fs.mkdir("/blaxel/import-only");
    await sandbox.fs.write("/blaxel/import-only/probe.txt", "first");
    await sandbox.fs.write("/blaxel/import-only/probe.txt", "second");
    assert.equal(await sandbox.fs.read("/blaxel/import-only/probe.txt"), "second");
    const listing = await sandbox.fs.ls("/blaxel/import-only");
    assert.ok(listing.files.some((entry) => entry.name === "probe.txt"));
    const reconnected = await SandboxInstance.get(sandbox.metadata.name!);
    assert.equal(await reconnected.fs.read("/blaxel/import-only/probe.txt"), "second");
    const started = await sandbox.process.exec({
      command: "cat; printf stdin-error >&2", name: "stdin-probe", stdin: true, waitForCompletion: false,
    });
    assert.equal(started.status, "running");
    assert.equal((await reconnected.process.get("stdin-probe")).pid, started.pid);
    await reconnected.process.writeStdin("stdin-probe", "hello λ\n");
    await reconnected.process.closeStdin("stdin-probe");
    const finished = await reconnected.process.wait("stdin-probe", { maxWait: 10_000, interval: 100 });
    assert.equal(finished.status, "completed");
    assert.equal(finished.stdout, "hello λ\n");
    assert.equal(finished.stderr, "stdin-error");
    assert.equal(await reconnected.process.logs("stdin-probe", "stdout"), "hello λ\n");
    assert.ok((await reconnected.process.list()).some((item) => item.name === "stdin-probe"));
    await sandbox.process.exec({ command: "sleep 30", name: "kill-probe", waitForCompletion: false });
    await assert.rejects(reconnected.process.wait("kill-probe", { maxWait: 1, interval: 1 }), /tim/i);
    assert.equal((await reconnected.process.get("kill-probe")).status, "running");
    await reconnected.process.kill("kill-probe");
    assert.equal((await reconnected.process.wait("kill-probe", { maxWait: 10_000, interval: 100 })).status, "killed");
  } finally {
    await sandbox.delete();
  }
}

if (!process.env.ARKER_API_KEY || !process.env.BL_API_KEY) throw new Error("Live Arker and Blaxel credentials required");
const directory = await mkdtemp(join(tmpdir(), "blaxel-import-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
try {
  for (const [provider, constructor] of [["blaxel", Native], ["blaxel", Shim], ["arker", Shim]] as const) {
    const config = join(directory, "router.json");
    await writeFile(config, JSON.stringify({
      version: 1,
      existing: { strategy: "priority", order: [provider] },
      create: { strategy: "priority", order: [provider] },
    }));
    process.env.COMPUTESDK_ROUTER_CONFIG = config;
    const called = new Set<string>();
    let outcome = "failed";
    try {
      await program(traceVendor(constructor, "SandboxInstance.static", called));
      outcome = "passed";
    } finally {
      await recordEvidence("blaxel", constructor === Native ? "reference" : provider === "arker" ? "arker" : "native",
        "import-only-files-and-command-isolation", [...called].sort(), outcome);
    }
    console.log(`PASS Blaxel unchanged program: ${constructor === Native ? "native SDK" : "shim"} on ${provider}`);
  }
} finally {
  if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
  else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
  await rm(directory, { recursive: true, force: true });
}
