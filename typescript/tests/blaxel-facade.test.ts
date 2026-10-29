import assert from "node:assert/strict";
import { test } from "node:test";
import { SandboxInstance } from "../src/blaxel.js";
import { createBlaxelDataPlane } from "../src/compat/blaxel-data-plane.js";

test("facade preserves native component identity and disposes on delete failure", async () => {
  const plane = createBlaxelDataPlane({connectPort: async () => {throw new Error("unexpected I/O");}});
  const box = {provider: "arker", sandboxId: "vm-test", getInstance: () => ({name: "test"}), destroy: async () => {throw new Error("delete failed");}};
  const facade = Reflect.construct(SandboxInstance, [box, undefined, undefined, plane]) as SandboxInstance;
  assert.equal(facade.fs, plane.fs);
  assert.equal(facade.process, plane.process);
  await assert.rejects(facade.delete(), /delete failed/);
  await assert.rejects(facade.fs.read("/file"), /closed/);
});

test("bootstrap retains both initialization and staging cleanup errors", async () => {
  const { bootstrapBlaxel } = await import("../src/compat/blaxel-bootstrap.js");
  const primary = new Error("ensure failed"), cleanup = new Error("cleanup failed");
  const vm = {
    id: "test", baseUrl: "https://custom.example/api",
    _client: { _request: async () => ({customer_paused: false}) },
    refresh: async () => ({policies: {rules: {}}}), sync: async () => {},
    run: async (command: string) => {
      if (command.includes("prepare")) return {exitCode: 0, stdout: '{"verified":true,"arch":"amd64"}'};
      throw command.startsWith("rm ") ? cleanup : primary;
    },
  };
  await assert.rejects(bootstrapBlaxel(vm as never), error => {
    assert.ok(error instanceof AggregateError);
    assert.deepEqual(error.errors, [primary, cleanup]);
    return true;
  });
});

test("failed creation retains initialization, cleanup and resource identity", async t => {
  const { setCompatComputeFactoryForTests } = await import("../src/compat/runtime.js");
  const primary = new Error("initialization failed"), cleanup = new Error("delete failed");
  const vm = {_client: {_request: async () => {throw primary;}}};
  const box = {provider: "arker", sandboxId: "vm-test", getInstance: () => vm,
    destroy: async () => {throw cleanup;}};
  setCompatComputeFactoryForTests(() => ({sandbox: {create: async () => box}}) as never);
  t.after(() => setCompatComputeFactoryForTests());
  await assert.rejects(SandboxInstance.create(), error => {
    assert(error instanceof AggregateError);
    assert.deepEqual(error.errors, [primary, cleanup]);
    assert.equal(Reflect.get(error, "sandboxId"), "vm-test");
    return true;
  });
});
