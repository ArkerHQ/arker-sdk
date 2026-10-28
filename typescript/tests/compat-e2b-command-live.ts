import assert from "node:assert/strict";
import { Sandbox as Native, CommandExitError, NotFoundError } from "e2b";
import { Sandbox as Shim } from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { traceVendor } from "./compat-trace.js";
import { withLiveRouter } from "./compat-live-router.js";

async function program(Sandbox: { create(): Promise<Native | Shim>; connect(id: string): Promise<Native | Shim>; kill(id: string): Promise<boolean> }) {
  const sandbox = await Sandbox.create();
  try {
    const connectedSandbox = await Sandbox.connect(sandbox.sandboxId);
    assert.equal(connectedSandbox.sandboxId, sandbox.sandboxId);
    assert.equal((await connectedSandbox.commands.run("printf connected")).stdout, "connected");
    const binary = Uint8Array.from({ length: 2048 }, (_, index) => index % 256);
    await sandbox.files.write("/tmp/compat-e2b-binary", binary.buffer);
    const binaryResult = await connectedSandbox.files.read("/tmp/compat-e2b-binary", { format: "bytes" });
    assert.equal(binaryResult.constructor, Uint8Array);
    assert.deepEqual(binaryResult, binary);
    assert.equal(await sandbox.files.read("/tmp/compat-e2b-binary"), new TextDecoder().decode(binary));
    const result = await sandbox.commands.run('printf "%s" "$COMPAT_VALUE"', {
      envs: { COMPAT_VALUE: "preserved" },
      timeoutMs: 10_000,
    });
    assert.deepEqual(
      [result.stdout, result.stderr, result.exitCode, result.error],
      ["preserved", "", 0, undefined],
    );
    await assert.rejects(
      () => sandbox.commands.run("printf output; printf failure >&2; exit 17"),
      (error: unknown) => {
        assert.ok(error instanceof CommandExitError);
        assert.deepEqual(
          [error.stdout, error.stderr, error.exitCode, error.error],
          ["output", "failure", 17, "exit status 17"],
        );
        return true;
      },
    );
    const handle = await sandbox.commands.run(
      "printf '%s\\n' $$; cat; printf diagnostic >&2; exit 17",
      { background: true, stdin: true },
    );
    await sandbox.commands.sendStdin(handle.pid, "hello\n");
    await sandbox.commands.closeStdin(handle.pid);
    await assert.rejects(handle.wait(), (error: unknown) => {
      assert.ok(error instanceof CommandExitError);
      assert.deepEqual(
        [error.stdout, error.stderr, error.exitCode, error.error],
        [String(handle.pid) + "\nhello\n", "diagnostic", 17, "exit status 17"],
      );
      return true;
    });
    const detached = await sandbox.commands.run("cat; printf reconnected", {
      background: true,
      stdin: true,
    });
    await detached.disconnect();
    const connected = await sandbox.commands.connect(detached.pid);
    assert.equal(connected.pid, detached.pid);
    await connected.sendStdin("shared\n");
    await connected.closeStdin();
    assert.equal((await connected.wait()).stdout, "shared\nreconnected");
    const killed = await sandbox.commands.run("cat", {
      background: true,
      stdin: true,
    });
    assert.equal(await killed.kill(), true);
    await assert.rejects(killed.wait(), (error: unknown) => {
      assert.ok(error instanceof CommandExitError);
      assert.deepEqual([error.exitCode, error.error], [-1, "signal: killed"]);
      return true;
    });
    assert.equal(await sandbox.commands.kill(killed.pid), false);
    assert.equal(await sandbox.commands.kill(2147483647), false);
    await assert.rejects(sandbox.commands.connect(2147483647), NotFoundError);
  } finally {
    assert.equal(await Sandbox.kill(sandbox.sandboxId), true);
    assert.equal(await sandbox.kill(), false);
    assert.equal(await Sandbox.kill(sandbox.sandboxId), false);
  }
}

await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY"], async ({ setProvider }) => {
  const nativeKey = process.env.E2B_API_KEY;
  for (const [provider, constructor] of [
    ["e2b", Native],
    ["e2b", Shim],
    ["arker", Shim],
  ] as const) {
    await setProvider(provider);
    if (provider === "arker") delete process.env.E2B_API_KEY;
    else process.env.E2B_API_KEY = nativeKey;
    const called = new Set<string>();
    let outcome = "failed";
    try {
      await program(traceVendor(constructor, "Sandbox.static", called));
      outcome = "passed";
    } finally {
      await recordEvidence(
        "e2b",
        constructor === Native
          ? "reference"
          : provider === "arker"
            ? "arker"
            : "native",
        "import-only-command-environment-and-exit",
        [...called].sort(),
        outcome,
      );
    }
    console.log(
      `PASS E2B environment and command exit: ${constructor === Native ? "native" : "shim"} on ${provider}`,
    );
  }
});
