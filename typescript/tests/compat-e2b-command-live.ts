import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Sandbox as Native, CommandExitError } from "e2b";
import { Sandbox as Shim } from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { traceVendor } from "./compat-trace.js";

async function program(Sandbox: { create(): Promise<Native | Shim> }) {
  const sandbox = await Sandbox.create();
  try {
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
  } finally {
    await sandbox.kill();
  }
}

if (!process.env.E2B_API_KEY || !process.env.ARKER_API_KEY)
  throw new Error("Live E2B and Arker credentials required");
const directory = await mkdtemp(join(tmpdir(), "compat-e2b-command-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
try {
  for (const [provider, constructor] of [
    ["e2b", Native],
    ["e2b", Shim],
    ["arker", Shim],
  ] as const) {
    const config = join(directory, "router.json");
    await writeFile(
      config,
      JSON.stringify({
        version: 1,
        existing: { strategy: "priority", order: [provider] },
        create: { strategy: "priority", order: [provider] },
      }),
    );
    process.env.COMPUTESDK_ROUTER_CONFIG = config;
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
} finally {
  if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
  else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
  await rm(directory, { recursive: true });
}
