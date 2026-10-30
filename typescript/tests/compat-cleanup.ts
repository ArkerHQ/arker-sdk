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

const { Cleanup } = await import("../scripts/compat-evidence.js");
const { SandboxNotFoundError, FileNotFoundError } = await import("e2b");
const { DaytonaNotFoundError, DaytonaForbiddenError } = await import("@daytona/sdk");
for (const provider of ["e2b", "daytona", "blaxel"] as const) {
  const collector = new Cleanup(provider);
  collector.allocated("owned");
  const absent = provider === "e2b" ? new SandboxNotFoundError("missing")
    : provider === "daytona" ? new DaytonaNotFoundError("missing", 404) : { code: 404, error: "missing" };
  const observe = (error: unknown) => collector.observeAbsent("owned", async id => {
    assert.equal(id, "owned"); throw error;
  });
  if (provider === "blaxel") {
    await assert.rejects(observe(absent), candidate => candidate === absent);
    await collector.observeAbsent("owned", async () => ({ response: new Response(null, { status: 404 }) }));
  } else await observe(absent);
  const qualified = () => events(collector.receipt()).map(item => ({ ...item, provider }));
  assert.deepEqual(verifiedScenarios("Sandbox.create", qualified(), "1", "code"), ["run:cleanup"]);
  for (const error of [new Error("404"), new FileNotFoundError("missing file"), new DaytonaForbiddenError("denied", 403),
    new DaytonaNotFoundError("contradictory", 403), { code: 403, error: "not found" },
    { code: 404, error: "denied", status_code: 403 }, { code: "404", error: "missing" }]) {
    await assert.rejects(observe(error), candidate => candidate === error);
    assert.deepEqual(verifiedScenarios("Sandbox.create", qualified(), "1", "code"), []);
  }
  await assert.rejects(collector.observeAbsent("owned", async () => ({ state: "paused" })), /absence has not/);
}
console.log("native absence contracts passed");

// Exercise Blaxel's actual generated HTTP decoder without credentials or networking.
const { getSandbox } = await import("@blaxel/core");
const { createRequire } = await import("node:module");
const require = createRequire(import.meta.url);
const nativeRequire = createRequire(require.resolve("@blaxel/core"));
const { createClient, createConfig } = nativeRequire("@hey-api/client-fetch");
for (const status of [404, 401, 403, 500]) {
  const collector = new Cleanup("blaxel");
  collector.allocated("owned");
  const requests: string[] = [];
  const client = createClient(createConfig({ baseUrl: "https://cleanup.invalid", fetch: async (request: Request) => {
    requests.push(request.url);
    return new Response(JSON.stringify({ code: 404, error: "misleading body: HTTP status must win" }), {
      status, headers: { "content-type": "application/json" },
    });
  } }));
  const observation = collector.observeAbsent("owned", sandboxName => getSandbox({ client, path: { sandboxName }, throwOnError: false }));
  if (status === 404) await observation;
  else await assert.rejects(observation, /absence has not/);
  assert.equal(requests.length, 1);
  assert.equal(new URL(requests[0]!).pathname, "/sandboxes/owned");
  const receiptEvents = events(collector.receipt()).map(item => ({ ...item, provider: "blaxel" }));
  assert.deepEqual(verifiedScenarios("Sandbox.create", receiptEvents, "1", "code"), status === 404 ? ["run:cleanup"] : []);
}
console.log("native Blaxel HTTP readback oracle passed");

// The shim's null lookup result has no HTTP envelope. Its decoded vendor error
// can only qualify the Arker route selected by the harness's isolated router.
const arkerBlaxel = new Cleanup("blaxel", "arker");
arkerBlaxel.allocated("owned");
await arkerBlaxel.observeAbsent("owned", async () => { throw { code: 404, error: "Sandbox not found" }; });
const arkerReceipt = arkerBlaxel.receipt();
for (const route of ["reference", "native", "arker"]) {
  const { cleanupObserved } = await import("../scripts/compat-evidence.js");
  assert.equal(cleanupObserved({ provider: "blaxel", route, cleanup: arkerReceipt }), route === "arker");
}
for (const error of [{ code: 403, error: "not found" }, { code: 404, error: "not found", status: 403 }]) {
  await assert.rejects(arkerBlaxel.observeAbsent("owned", async () => { throw error; }), candidate => candidate === error);
}
