import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import * as Native from "e2b";
import * as Shim from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";
import { traceVendor } from "./compat-trace.js";

const cases = ["create-0", "create-86401", "connect-running-0", "connect-running-86401", "connect-paused-0", "connect-paused-86401", "setter-negative", "setter-0", "setter-86401"] as const;
type Case = (typeof cases)[number];

// The session cap is the tested account's 24-hour profile.
export async function program(Sandbox: typeof Native.Sandbox, scenario: Case): Promise<void> {
  const seconds = scenario.endsWith("negative") ? -1 : scenario.endsWith("86401") ? 86401 : 0;
  const badRequest = (error: unknown) => error instanceof Native.SandboxError
    && error.constructor === Native.SandboxError && error.statusCode === 400;
  let sandbox: Native.Sandbox | undefined;
  try {
    if (scenario.startsWith("create")) {
      await assert.rejects(async () => { sandbox = await Sandbox.create({ timeoutMs: seconds * 1000 }); }, badRequest);
      return;
    }
    sandbox = await Sandbox.create();
    if (scenario.startsWith("connect-paused")) assert.equal(await sandbox.pause(), true);
    const before = await sandbox.getInfo();
    if (scenario.startsWith("connect")) {
      await assert.rejects(Sandbox.connect(sandbox.sandboxId, { timeoutMs: seconds * 1000 }), badRequest);
      const after = await sandbox.getInfo();
      assert.deepEqual([after.state, after.startedAt, after.endAt], [before.state, before.startedAt, before.endAt]);
    } else if (seconds < 0) {
      await assert.rejects(sandbox.setTimeout(seconds * 1000), badRequest);
      assert.deepEqual((await sandbox.getInfo()).endAt, before.endAt);
    } else if (seconds === 0) {
      assert.equal(await sandbox.setTimeout(0), undefined);
      await assert.rejects(sandbox.getInfo(), error => error instanceof Native.SandboxNotFoundError
        && error.constructor === Native.SandboxNotFoundError);
      assert.equal(await sandbox.isRunning(), false);
      assert.equal(await sandbox.kill(), false);
    } else {
      await new Promise(resolve => setTimeout(resolve, 1100));
      assert.equal(await sandbox.setTimeout(seconds * 1000), undefined);
      const after = await sandbox.getInfo();
      assert.deepEqual(after.startedAt, before.startedAt);
      assert.equal(after.endAt.getTime() - after.startedAt.getTime(), 86_400_000);
    }
  } finally { if (sandbox) await sandbox.kill(); }
}

async function main(): Promise<void> {
  const selected = process.env.COMPAT_E2B_CASES?.split(",") ?? [...cases];
  for (const name of selected) assert.ok(cases.includes(name as Case), `Unknown timeout boundary scenario: ${name}`);
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
      for (const scenario of selected as Case[]) {
        const called = new Set<string>();
        let outcome = "failed";
        try {
          await program(traceVendor(module.Sandbox as unknown as typeof Native.Sandbox, "Sandbox.static", called), scenario);
          outcome = "passed";
        } catch (error) {
          console.error(`Timeout boundary ${scenario} failed on ${provider}: ${error instanceof Error ? error.name : typeof error}`);
          failures.push(new Error(`Timeout boundary ${scenario}: ${provider}/${module === Native ? "reference" : "shim"} failed`));
        } finally {
          await recordEvidence("e2b", module === Native ? "reference" : provider === "arker" ? "arker" : "native",
            `import-only-timeout-boundary-${scenario}`, [...called].sort(), outcome);
        }
      }
    }
    if (failures.length) throw new AggregateError(failures, "Timeout boundary differential failed; inspect evidence rows");
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
