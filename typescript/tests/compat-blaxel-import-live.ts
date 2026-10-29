import assert from "node:assert/strict";
import { writeFile, readFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { withLiveRouter } from "./compat-live-router.js";

import { SandboxInstance as Native } from "@blaxel/core";
import { SandboxInstance as Shim } from "../src/blaxel.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { traceVendor } from "./compat-trace.js";

// This program uses only official Blaxel calls; the harness changes the import.
async function program(SandboxInstance: typeof Native | typeof Shim, directory: string): Promise<void> {
  const sandboxName = `arker-import-${randomUUID().slice(0, 12)}`;
  const sandbox = await SandboxInstance.create({ name: sandboxName });
  try {
    assert.equal(sandbox.metadata?.name, sandboxName);
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
    await sandbox.process.exec({
      command: "sleep 0.2; printf within-deadline", name: "seconds-timeout", timeout: 1,
      waitForCompletion: false,
    });
    const withinDeadline = await sandbox.process.wait("seconds-timeout", { maxWait: 10_000, interval: 100 });
    assert.deepEqual([withinDeadline.status, withinDeadline.exitCode, withinDeadline.stdout],
      ["completed", 0, "within-deadline"]);
    await sandbox.process.exec({
      command: "while :; do sleep 1; done", name: "execution-deadline", timeout: 1,
      waitForCompletion: false,
    });
    const expired = await sandbox.process.wait("execution-deadline", { maxWait: 10_000, interval: 100 });
    assert.deepEqual([expired.status, expired.exitCode], ["killed", -1]);
    const failed = await sandbox.process.exec({
      command: "printf failure-out; printf failure-err >&2; exit 17", waitForCompletion: true,
    });
    assert.deepEqual([failed.stdout, failed.stderr, failed.exitCode, failed.status], ["failure-out", "failure-err", 17, "failed"]);
    assert.deepEqual(await sandbox.fs.mkdir("/blaxel/import-only"), {
      message: "Directory created successfully", path: "/blaxel/import-only",
    });
    await sandbox.fs.write("/blaxel/import-only/probe.txt", "first");
    await sandbox.fs.write("/blaxel/import-only/probe.txt", "second");
    assert.equal(await sandbox.fs.read("/blaxel/import-only/probe.txt"), "second");
    const listing = await sandbox.fs.ls("/blaxel/import-only");
    assert.equal(listing.name, "import-only");
    assert.equal(listing.path, "/blaxel/import-only");
    assert.ok(listing.files.some((entry) => entry.name === "probe.txt"));
    assert.equal(listing.files.find((entry) => entry.name === "probe.txt")?.path,
      "/blaxel/import-only/probe.txt");
    for (const [path, display] of [
      ["/blaxel/import-only/", "/blaxel/import-only/"],
      ["/blaxel//import-only", "/blaxel/import-only"],
    ]) {
      const absolute = await sandbox.fs.ls(path!);
      assert.equal(absolute.name, "import-only");
      assert.equal(absolute.path, display);
      assert.equal(absolute.files.find((entry) => entry.name === "probe.txt")?.path,
        "/blaxel/import-only/probe.txt");
    }
    const literalPath = `~/${sandboxName}`;
    assert.equal((await sandbox.fs.mkdir(literalPath)).path, `/${literalPath}`);
    assert.equal((await sandbox.fs.write(`${literalPath}/a`, "literal")).path, `/${literalPath}/a`);
    assert.equal(await sandbox.fs.read(`${literalPath}/a`), "literal");
    assert.equal(await (await sandbox.fs.readBinary(`${literalPath}/a`)).text(), "literal");
    assert.equal((await sandbox.fs.ls(literalPath)).path, `/${literalPath}`);
    assert.equal((await sandbox.fs.writeTree([{ path: "b", content: "tree" }], literalPath))?.path, `/${literalPath}`);
    assert.equal(await sandbox.fs.read(`/${literalPath}/b`), "tree");
    await sandbox.fs.rm(`${literalPath}/a`);
    await sandbox.fs.rm(literalPath, true);
    const root = await sandbox.fs.ls("/");
    assert.equal(root.name, "/");
    assert.equal(root.path, "/");
    const binaryPath = "/blaxel/import-only/binary";
    const payload = Uint8Array.from({ length: 256 }, (_, i) => i);
    assert.deepEqual(await sandbox.fs.writeBinary(binaryPath, payload), {
      message: "Binary file uploaded successfully", path: binaryPath,
    });
    assert.deepEqual(new Uint8Array(await (await sandbox.fs.readBinary(binaryPath)).arrayBuffer()), payload);
    await sandbox.fs.writeBinary(binaryPath, new Blob([payload]));
    const localPath = join(directory, "binary-input");
    await writeFile(localPath, payload);
    await sandbox.fs.writeBinary(binaryPath, localPath);
    const downloaded = join(directory, "binary-download");
    await sandbox.fs.download(binaryPath, downloaded, { mode: 0o600 });
    assert.deepEqual(new Uint8Array(await readFile(downloaded)), payload);
    assert.equal((await stat(downloaded)).mode & 0o777, 0o600);
    await sandbox.fs.writeBinary(binaryPath, new Uint8Array());
    assert.equal((await sandbox.fs.readBinary(binaryPath)).size, 0);
    await assert.rejects(() => sandbox.fs.rm("/blaxel/import-only"));
    assert.equal(await sandbox.fs.read("/blaxel/import-only/probe.txt"), "second");
    assert.deepEqual(await sandbox.fs.rm(binaryPath), { message: "File deleted successfully", path: binaryPath });
    const reconnected = await SandboxInstance.get(sandbox.metadata.name!);
    assert.equal(await reconnected.fs.read("/blaxel/import-only/probe.txt"), "second");
    const unattendedPath = "/blaxel/import-only/unattended";
    const unattended = await sandbox.process.exec({
      command: `sleep 2; printf done > ${unattendedPath}`, name: "unattended", waitForCompletion: false,
    });
    assert.equal(unattended.status, "running");
    await new Promise((resolve) => setTimeout(resolve, 8_000));
    assert.equal(await sandbox.fs.read(unattendedPath), "done");
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

await withLiveRouter(["ARKER_API_KEY", "BL_API_KEY"], async ({ directory, setProvider }) => {
  for (const [provider, constructor] of [["blaxel", Native], ["blaxel", Shim], ["arker", Shim]] as const) {
    await setProvider(provider);
    const called = new Set<string>();
    let outcome = "failed";
    try {
      await program(traceVendor(constructor, "SandboxInstance.static", called), directory);
      outcome = "passed";
    } finally {
      await recordEvidence("blaxel", constructor === Native ? "reference" : provider === "arker" ? "arker" : "native",
        "import-only-files-and-command-isolation", [...called].sort(), outcome);
    }
    console.log(`PASS Blaxel unchanged program: ${constructor === Native ? "native SDK" : "shim"} on ${provider}`);
  }
});
