import { createRequire } from "node:module";
import assert from "node:assert/strict";
import { test } from "node:test";
import { Arker, type VM, type ForkOptions } from "../src/index.js";
import { ModalClient } from "../src/modal.js";
import { preserveEnvironment } from "./provider-fixtures.js";

for (const blockNetwork of [false, true]) {
  test(`Modal default lifetime is atomic and keeps custom policies (blocked=${blockNetwork})`, async t => {
    preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
    delete process.env.COMPUTESDK_ROUTER_CONFIG;
    const calls: ForkOptions[] = [];
    t.mock.method(Arker.prototype, "fork", async (request: ForkOptions) => {
      calls.push(request); return { id: "owned" } as VM;
    });
    const policies: NonNullable<ForkOptions["policies"]> = { rules: { custom: { type: "inbound", priority: 1, action: "deny" } }, secrets: { TOKEN: "synthetic" } };
    const before = structuredClone(policies);
    const client = new ModalClient({ arker: { apiKey: "synthetic", source: "fixture", policies } });
    const sandbox = await client.sandboxes.create({ blockNetwork });
    assert.equal(sandbox.sandboxId, "owned");
    assert.equal(calls.length, 1);
    const actual = calls[0]!.policies!;
    assert.deepEqual(actual.rules!["computesdk-modal-lifetime"], { type: "schedule", after_created: 300, action: "delete" });
    assert.equal(actual.rules!["computesdk-e2b-lifetime"], undefined);
    assert.deepEqual(actual.rules!.custom, before.rules!.custom);
    assert.deepEqual(actual.secrets, before.secrets);
    assert.equal(Object.values(actual.rules!).filter(rule => rule?.type === "outbound_guard").length, Number(blockNetwork));
    assert.deepEqual(policies, before);
  });
}

test("Modal rejects conflicting rules before fork", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const fork = t.mock.method(Arker.prototype, "fork", async () => { throw new Error("must not fork"); });
  for (const policies of [{ rules: { "computesdk-modal-lifetime": null } }, { policies: [] }]) {
    const client = new ModalClient({ arker: { apiKey: "synthetic", source: "fixture", policies } });
    await assert.rejects(client.sandboxes.create(), /reserved|named policies/);
  }
  assert.equal(fork.mock.callCount(), 0);
});

for (const timeoutMs of [10_000, 600_000, 86_400_000]) {
  test(`Modal explicit lifetime ${timeoutMs}`, async t => {
    preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
    delete process.env.COMPUTESDK_ROUTER_CONFIG;
    const calls: ForkOptions[] = [];
    t.mock.method(Arker.prototype, "fork", async (request: ForkOptions) => {
      calls.push(request); return { id: "owned" } as VM;
    });
    const client = new ModalClient({ arker: { apiKey: "synthetic", source: "fixture" } });
    await client.sandboxes.create({ timeoutMs });
    assert.deepEqual(calls[0]!.policies!.rules!["computesdk-modal-lifetime"], {
      type: "schedule", after_created: timeoutMs / 1000, action: "delete",
    });
  });
}

test("Modal invalid lifetimes fail before fork with native errors", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const require = createRequire(typeof import.meta.url === "string" ? import.meta.url : __filename);
  const { ClientError, Status } = createRequire(require.resolve("modal"))("nice-grpc");
  const fork = t.mock.method(Arker.prototype, "fork", async () => { throw new Error("must not fork"); });
  const client = new ModalClient({ arker: { apiKey: "synthetic", source: "fixture" } });
  for (const timeoutMs of [0, -1000]) {
    await assert.rejects(client.sandboxes.create({ timeoutMs }), { name: "Error", message: `timeoutMs must be positive, got ${timeoutMs}` });
  }
  for (const timeoutMs of [1.5, 10_001]) {
    await assert.rejects(client.sandboxes.create({ timeoutMs }), { name: "Error", message: `timeoutMs must be a multiple of 1000ms, got ${timeoutMs}` });
  }
  for (const timeoutMs of [1000, 9000, 86_401_000]) {
    await assert.rejects(client.sandboxes.create({ timeoutMs }), (error: unknown) => {
      assert.ok(error instanceof ClientError);
      assert.equal((error as { code: number }).code, Status.INVALID_ARGUMENT);
      return true;
    });
  }
  assert.equal(fork.mock.callCount(), 0);
});
