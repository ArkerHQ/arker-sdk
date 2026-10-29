import assert from "node:assert/strict";
import { test } from "node:test";
import { Sandbox } from "../src/e2b.js";
import { ArkerError } from "../src/index.js";
import { setCompatComputeFactoryForTests } from "../src/compat/runtime.js";

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

test("network initialization failure deletes only the new VM before bootstrap", async t => {
  const factory = Sandbox as unknown as { dataPlane: () => Promise<unknown> };
  const original = factory.dataPlane;
  t.after(() => { factory.dataPlane = original; setCompatComputeFactoryForTests(); });
  const failure = new Error("policy read failed");
  let deleted = 0;
  factory.dataPlane = async () => { throw new Error("bootstrap must not run"); };
  const box = { provider: "arker", sandboxId: "new-sandbox", getInstance: () => ({
    refresh: async () => { throw failure; }, delete: async () => { deleted++; },
  }) };
  setCompatComputeFactoryForTests((() => ({ sandbox: { create: async () => box } })) as never);
  await assert.rejects(Sandbox.create(), error => error === failure);
  assert.equal(deleted, 1);
});
