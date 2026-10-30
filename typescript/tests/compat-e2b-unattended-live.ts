import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import * as Native from "e2b";
import * as Shim from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";
import { traceVendor } from "./compat-trace.js";

export async function program(Sandbox: typeof Native.Sandbox): Promise<void> {
  const owned: Native.Sandbox[] = [], errors: unknown[] = [];
  const quiet = async (box: Native.Sandbox) => {
    const path = `/home/user/quiet-${randomUUID()}`;
    const command = `import json,time,pathlib; start=time.time(); time.sleep(2); pathlib.Path(${JSON.stringify(path)}).write_text(json.dumps([start,time.time()]))`;
    const handle = await box.commands.run(`python3 -c '${command}'`, {background:true,timeoutMs:30_000});
    handle.disconnect();
    // Do not poll anything: requests can hide an idle-VM activity gap.
    await delay(8_000);
    const [start, end] = JSON.parse(await box.files.read(path));
    assert.ok(end-start >= 1.5 && end-start < 5, 'Work resumed only when the client returned');
  };
  try {
    let box = await Sandbox.create({timeoutMs:180_000}); owned.push(box);
    await quiet(box);
    box = await Sandbox.connect(box.sandboxId); await quiet(box);
    for (const mode of ['full','filesystem'] as const) {
      assert.equal(await box.pause({mode}), true);
      assert.equal(await box.isRunning(), false);
      box = await Sandbox.connect(box.sandboxId); await quiet(box);
    }
    const children = await box.fork({count:1,timeoutMs:60_000});
    owned.push(...children.filter((child): child is Native.Sandbox => !(child instanceof Error)));
    assert.equal(children.length, 1);
    const child = children[0]!;
    if (child instanceof Error) throw child;
    await quiet(child); await quiet(box);
    await box.setTimeout(3_000); await delay(6_000);
    assert.equal(await box.isRunning(), false, 'Lifetime run prevented expiry');
  } catch (error) { errors.push(error); }
  finally {
    for (const box of owned.reverse()) {
      try { await box.kill(); assert.equal(await box.isRunning(), false); }
      catch (error) { errors.push(error); }
    }
  }
  if (errors.length) throw new AggregateError(errors, 'Unattended differential or cleanup failed');
}

async function main(): Promise<void> {
  const routes = process.env.COMPAT_E2B_ROUTES?.split(",") ?? ["reference", "native", "arker"];
  assert.ok(routes.every(route => ["reference", "native", "arker"].includes(route)));
  await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"], async ({ setProvider }) => {
    assert.ok(process.env.ARKER_SOURCE || process.env.ARKER_SOURCE_VM || process.env.ARKER_SOURCE_VM_ID);
    const key = process.env.E2B_API_KEY, errors: unknown[] = [];
    for (const route of routes) {
      await setProvider(route === "arker" ? "arker" : "e2b");
      if (route === "arker") delete process.env.E2B_API_KEY; else process.env.E2B_API_KEY = key;
      let outcome = "failed";
      const calls = new Set<string>();
      try {
        await program(traceVendor((route === "reference" ? Native.Sandbox : Shim.Sandbox) as typeof Native.Sandbox, "Sandbox.static", calls));
        outcome = "passed";
      } catch (error) { errors.push(error); }
      finally {
        await recordEvidence("e2b", route, "import-only-unattended", [...calls].sort(), outcome);
      }
    }
    if (errors.length) throw new AggregateError(errors, "Unattended differential routes failed");
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
