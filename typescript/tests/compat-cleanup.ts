import assert from "node:assert/strict";
import { ModalCleanup, verifiedScenarios } from "../scripts/compat-evidence.js";

const events = (cleanup: unknown) => ["reference", "native", "arker"].map(route => ({
  provider: "modal", run: "run", scenario: "cleanup", route, outcome: "passed",
  vendor_version: "1", implementation_fingerprint: "code", apis: ["Sandbox.create"], cleanup,
}));
const verified = (cleanup: unknown) => verifiedScenarios("Sandbox.create", events(cleanup), "1", "code");
for (const receipt of [undefined, true, { verified: true }, { cleanupRequested: true }]) {
  assert.deepEqual(verified(receipt), []);
}
const cleanup = new ModalCleanup();
cleanup.allocated("one"); cleanup.allocated("two");
const calls: string[] = [];
const lookup = async (sandboxId: string) => {
  calls.push(`lookup:${sandboxId}`);
  return { sandboxId, poll: async () => { calls.push(`poll:${sandboxId}`); return 137; } };
};
await cleanup.observe("one", lookup);
assert.deepEqual(verified(cleanup.receipt()), []);
await cleanup.observe("two", lookup);
assert.deepEqual(verified(cleanup.receipt()), ["run:cleanup"]);
assert.deepEqual(calls, ["lookup:one", "poll:one", "lookup:two", "poll:two"]);
const history = [...events(cleanup.receipt()), { ...events(undefined)[0]!, outcome: "failed", apis: [] }];
const saved = JSON.stringify(history);
assert.deepEqual(verifiedScenarios("Sandbox.create", history, "1", "code"), []);
assert.equal(JSON.stringify(history), saved);
for (const result of [null, true, "137", 0.5]) {
  await assert.rejects(cleanup.observe("one", async sandboxId => ({ sandboxId, poll: async () => result as number })), /not been observed/);
  assert.deepEqual(verified(cleanup.receipt()), []);
}
await cleanup.observe("one", lookup);
const error = new Error("transport");
await assert.rejects(cleanup.observe("one", async () => { throw error; }), candidate => candidate === error);
assert.deepEqual(verified(cleanup.receipt()), []);
await assert.rejects(cleanup.observe("one", async () => ({ sandboxId: "other", poll: async () => assert.fail("wrong identity") })), /different/);
await assert.rejects(cleanup.observe("unknown", lookup), /enrolled/);
assert.throws(() => cleanup.allocated("one"), /new sandbox/);
console.log("cleanup receipt protocol tests passed");
