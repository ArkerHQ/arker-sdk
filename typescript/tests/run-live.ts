// Live checks of run results the unit tests' recorded responses do not cover.
// Run with ARKER_API_KEY set (and ARKER_BASE_URL, or ARKER_PROVIDER and ARKER_REGION).
import assert from "node:assert/strict";

import { Arker } from "../src/index.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP run live: set ARKER_API_KEY to run");
  process.exit(0);
}

const vm = await new Arker().fork({ source_vm_name: process.env.ARKER_SOURCE_VM ?? "ubuntu-coding" });
try {
  // A prompt ends the run without an exit code.
  const run = await vm.run("echo READY; sleep 30", { end_symbol: "READY" });
  assert.equal(run.type, "completed");
  if (run.type !== "completed") throw new Error("unreachable");
  assert.deepEqual([run.state, run.exitCode], ["completed", null]);
  assert.match(run.stdout, /READY/);
} finally {
  await vm.delete();
}

console.log("PASS run live");
