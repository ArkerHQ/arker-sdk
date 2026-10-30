import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import * as Native from "e2b";
import * as Shim from "../src/e2b.js";
import { Arker } from "../src/index.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";
import { traceVendor } from "./compat-trace.js";

const cases = ["running-timeout", "paused-timeout", "repeated-pause", "deleted-state", "paused-command", "paused-read", "paused-pty"] as const;
type Case = (typeof cases)[number];

// Import substitution changes only Sandbox; errors must retain native identity.
export async function program(
  Sandbox: typeof Native.Sandbox,
  scenario: Case,
  assertDeadline: (id: string, present: boolean) => Promise<void>,
): Promise<void> {
  const sandbox = await Sandbox.create();
  const notFound = (error: unknown) => error instanceof Native.SandboxNotFoundError
    && error.constructor === Native.SandboxNotFoundError;
  try {
    if (["paused-command", "paused-read", "paused-pty"].includes(scenario)) {
      const marker = `/tmp/paused-admission-${randomUUID()}`;
      await sandbox.files.write(`${marker}-input`, "paused-read-content");
      const before = (await sandbox.commands.list()).map(process => process.pid).sort();
      await assertDeadline(sandbox.sandboxId, true);
      assert.equal(await sandbox.pause(), true);
      let output = false;
      let refusal: unknown;
      try {
        if (scenario === "paused-command") await sandbox.commands.run(`touch ${marker}`, {
          requestTimeoutMs: 15_000, onStdout() { output = true; }, onStderr() { output = true; },
        });
        if (scenario === "paused-read") await sandbox.files.read(`${marker}-input`, { requestTimeoutMs: 15_000 });
        if (scenario === "paused-pty") await sandbox.pty.create({
          rows: 24, cols: 80, timeoutMs: 0, requestTimeoutMs: 15_000, onData() { output = true; },
        });
      } catch (error) { refusal = error; }
      assert.equal((await sandbox.getInfo()).state, "paused");
      assert.equal(await sandbox.isRunning(), false);
      await assertDeadline(sandbox.sandboxId, false);
      assert.equal(output, false);
      const resumed = await Sandbox.connect(sandbox.sandboxId);
      assert.deepEqual((await resumed.commands.list()).map(process => process.pid).sort(), before);
      assert.equal(await resumed.files.exists(marker), false);
      assert.equal(await resumed.files.read(`${marker}-input`), "paused-read-content");
      assert.ok(refusal instanceof Native.SandboxNotRunningError);
      assert.equal(refusal.constructor, Native.SandboxNotRunningError);
    } else if (scenario === "running-timeout") {
      const before = await sandbox.getInfo();
      assert.equal(await sandbox.setTimeout(120_000), undefined);
      const shorter = await sandbox.getInfo();
      assert.equal(shorter.state, "running");
      assert.equal(shorter.startedAt.getTime(), before.startedAt.getTime());
      assert.ok(shorter.endAt < before.endAt);
      assert.equal(await sandbox.setTimeout(240_000), undefined);
      const longer = await sandbox.getInfo();
      assert.equal(longer.startedAt.getTime(), before.startedAt.getTime());
      assert.ok(longer.endAt > shorter.endAt);
      await assertDeadline(sandbox.sandboxId, true);
    } else if (scenario === "deleted-state") {
      assert.equal(await sandbox.kill(), true);
      assert.equal(await sandbox.isRunning(), false);
      await assert.rejects(sandbox.getInfo(), notFound);
      assert.equal(await sandbox.kill(), false);
    } else {
      await assertDeadline(sandbox.sandboxId, true);
      assert.equal(await sandbox.pause(), true);
      assert.equal(await sandbox.isRunning(), false);
      await assertDeadline(sandbox.sandboxId, false);
      if (scenario === "paused-timeout") {
        try {
          await assert.rejects(sandbox.setTimeout(120_000), notFound);
        } finally {
          await assertDeadline(sandbox.sandboxId, false);
        }
      } else {
        assert.equal(await sandbox.pause(), false);
        await assertDeadline(sandbox.sandboxId, false);
        assert.equal(await sandbox.pause(), false);
        assert.equal((await sandbox.getInfo()).state, "paused");
      }
      assert.equal(await sandbox.isRunning(), false);
      await assertDeadline(sandbox.sandboxId, false);
    }
  } finally {
    await sandbox.kill();
  }
}

async function main(): Promise<void> {
  const selected = process.env.COMPAT_E2B_CASES?.split(",") ?? [...cases];
  for (const name of selected) assert.ok(cases.includes(name as Case), `Unknown lifecycle scenario: ${name}`);
  const selectedRoutes = process.env.COMPAT_E2B_ROUTES?.split(",") ?? ["reference", "native", "arker"];
  for (const route of selectedRoutes) assert.ok(["reference", "native", "arker"].includes(route), `Unknown lifecycle route: ${route}`);
  await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"], async ({ setProvider }) => {
    assert.ok(process.env.ARKER_SOURCE || process.env.ARKER_SOURCE_VM || process.env.ARKER_SOURCE_VM_ID, "An isolated source is required");
    const nativeKey = process.env.E2B_API_KEY;
    const failures: Error[] = [];
    for (const [provider, module] of [["e2b", Native], ["e2b", Shim], ["arker", Shim]] as const) {
      const route = module === Native ? "reference" : provider === "arker" ? "arker" : "native";
      if (!selectedRoutes.includes(route)) continue;
      await setProvider(provider);
      if (provider === "arker") delete process.env.E2B_API_KEY;
      else process.env.E2B_API_KEY = nativeKey;
      const assertDeadline = async (id: string, present: boolean) => {
        if (provider !== "arker") return;
        const current = await new Arker().vm(id).refresh();
        assert.ok(current.policies, "VM response omitted effective policies");
        assert.equal(Object.hasOwn(current.policies.rules ?? {}, "computesdk-e2b-lifetime"), present,
          "Running deadline must exist before pause and remain absent afterward");
      };
      for (const scenario of selected as Case[]) {
        const called = new Set<string>();
        let outcome = "failed";
        try {
          await program(traceVendor(module.Sandbox as unknown as typeof Native.Sandbox, "Sandbox.static", called), scenario, assertDeadline);
          outcome = "passed";
        } catch (error) {
          console.error(`Lifecycle ${scenario} failed on ${provider}: ${error instanceof Error ? error.name : typeof error}`);
          failures.push(new Error(`Lifecycle ${scenario}: ${provider}/${module === Native ? "reference" : "shim"} failed`));
        } finally {
          await recordEvidence("e2b", module === Native ? "reference" : provider === "arker" ? "arker" : "native",
            `import-only-lifecycle-${scenario}`, [...called].sort(), outcome);
        }
      }
    }
    if (failures.length) throw new AggregateError(failures, "Lifecycle differential failed; inspect evidence rows");
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
