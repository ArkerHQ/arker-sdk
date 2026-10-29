import assert from "node:assert/strict";
import { test } from "node:test";
import { Sandbox } from "../src/e2b.js";
import { Arker, ArkerError, VM } from "../src/index.js";
import { setCompatComputeFactoryForTests } from "../src/compat/runtime.js";
import { preserveEnvironment, preserveProperties } from "./provider-fixtures.js";

for (const outcome of ["deleted", "missing", "failure"] as const) {
  test(`kill closes only the owned data plane when ${outcome}`, async () => {
    const events: string[] = [];
    const failure = new Error("delete failed");
    const vm = {
      async refresh() { throw new Error("delete must not issue a preliminary read"); },
      async delete() { events.push("delete"); if (outcome === "missing") throw new ArkerError("not_found", "missing", 404); if (outcome === "failure") throw failure; return { deleted: true }; },
    };
    const plane = { commands: {}, files: {}, pty: {}, git: {}, close() { events.push("close"); } };
    const sandbox = Reflect.construct(Sandbox, [{ provider: "arker", sandboxId: "sandbox", getInstance: () => vm }, undefined, undefined, plane]) as Sandbox;
    if (outcome === "failure") await assert.rejects(sandbox.kill(), error => error === failure);
    else assert.equal(await sandbox.kill(), outcome === "deleted");
    assert.equal(events.at(-1), "close");
    assert.equal(events.filter(event => event === "close").length, 1);
  });
}

test("native kill does not dispose unrelated data-plane state", async () => {
  const native = { kill: async () => true };
  const sandbox = Reflect.construct(Sandbox, [{ provider: "e2b", sandboxId: "sandbox" }, native, undefined,
    { close() { throw new Error("unrelated pool"); } }]) as Sandbox;
  assert.equal(await sandbox.kill(), true);
});

test("failed data-plane initialization deletes the newly created VM", async (t) => {
  const factory = Sandbox as unknown as { dataPlane: () => Promise<unknown> };
  const original = factory.dataPlane;
  t.after(() => { factory.dataPlane = original; setCompatComputeFactoryForTests(); });
  let deleted = 0;
  const failure = new Error("bootstrap failed");
  factory.dataPlane = async () => { throw failure; };
  const box = { provider: "arker", sandboxId: "sandbox", runCommand: async () => ({ exitCode: 0 }),
    getInstance: () => ({ refresh: async () => ({ policies: { rules: {} } }), update: async () => {}, delete: async () => { deleted++; } }) };
  setCompatComputeFactoryForTests((() => ({ sandbox: { create: async () => box } })) as never);
  await assert.rejects(Sandbox.create(), error => error === failure);
  assert.equal(deleted, 1);
});

test("source policy failure prevents fork and bootstrap without deleting any VM", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_SOURCE_VM_ID", "COMPUTESDK_ROUTER_CONFIG");
  preserveProperties(t, Arker.prototype, "getVm", "fork");
  preserveProperties(t, VM.prototype, "delete");
  process.env.ARKER_API_KEY = "synthetic-key";
  process.env.ARKER_SOURCE_VM_ID = "00000000000000000000000000";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const factory = Sandbox as unknown as { dataPlane: () => Promise<unknown> };
  const original = factory.dataPlane;
  t.after(() => { factory.dataPlane = original; });
  const failure = new Error("policy read failed");
  const events: string[] = [];
  Arker.prototype.getVm = async () => { events.push("source-policy"); throw failure; };
  Arker.prototype.fork = async () => { events.push("fork"); throw new Error("fork must not run"); };
  VM.prototype.delete = async () => { events.push("delete"); throw new Error("no VM to delete"); };
  factory.dataPlane = async () => { events.push("bootstrap"); throw new Error("bootstrap must not run"); };
  await assert.rejects(Sandbox.create(), error => error === failure);
  assert.deepEqual(events, ["source-policy"]);
});
