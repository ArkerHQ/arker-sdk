import assert from "node:assert/strict";
import { test } from "node:test";
import { InvalidArgumentError, RateLimitError } from "e2b";
import { forkArker } from "../src/compat/e2b-fork.js";
import { trafficPolicies, trafficToken } from "../src/compat/e2b-network.js";
import { ArkerError, type VM } from "../src/index.js";
import { Sandbox } from "../src/e2b.js";

function fixture() {
  const calls: Array<[string, any]> = [];
  const children = [0, 1].map(id => ({ id: `child-${id}`, async delete() { calls.push(["delete", id]); } } as unknown as VM));
  let index = 0;
  const checkpoint = {
    async update(options: unknown) { calls.push(["freeze", options]); },
    async fork(options: unknown) { calls.push(["child", options]); return children[index++]; },
    async delete() { calls.push(["checkpoint-delete", null]); },
  };
  const policies = trafficPolicies(undefined, undefined, false).policies;
  const parent = {
    id: "parent",
    metadata: { arker_e2b: { version: 1, metadata: { team: "test" }, source: { kind: "vm_name", value: "source" }, envd_version: "0.10.2" } },
    policies,
    async refresh() { return this; },
    async fork(options: unknown) { calls.push(["capture", options]); return checkpoint; },
  } as unknown as VM;
  return { parent, checkpoint, children, calls };
}

test("one capture; distinct atomic private tokens; checkpoint is never executed", async () => {
  const { parent, children, calls } = fixture();
  const results = await forkArker(parent, async vm => vm, vm => vm.delete(), { count: 2, timeoutMs: 42001 });
  assert.deepEqual(results, children);
  assert.deepEqual(calls.map(([name]) => name), ["capture", "child", "child", "checkpoint-delete"]);
  assert.equal(calls[0][1].immutable, true);
  assert.deepEqual(calls[0][1].metadata, { arker_e2b_checkpoint: { version: 1, source_vm_id: "parent" } });
  assert.equal(calls[0][1].policies.rules["computesdk-e2b-apps"], null);
  const secrets = calls.filter(([name]) => name === "child").map(([, opts]) => {
    assert.deepEqual(opts.metadata, parent.metadata);
    assert.equal(opts.policies.rules["computesdk-e2b-lifetime"].after_created, 43);
    return opts.policies.secrets["computesdk-e2b-traffic"];
  });
  assert.equal(new Set([...secrets, parent.policies!.secrets!["computesdk-e2b-traffic"]]).size, 3);
  assert.deepEqual(children.map(trafficToken), secrets);
});

test("child readiness failure cleans only that child and preserves per-child errors", async () => {
  const { parent, children, calls } = fixture();
  const values = await forkArker(parent, async vm => {
    if (vm === children[0]) throw new ArkerError("capacity_unavailable", "capacity", 429);
    return vm;
  }, vm => vm.delete(), { count: 2 });
  assert.ok(values[0] instanceof RateLimitError);
  assert.equal(values[1], children[1]);
  assert.deepEqual(calls.filter(([name]) => name === "delete"), [["delete", 0]]);
});

test("invalid counts are rejected before capture", async () => {
  for (const count of [0, 21, 1.5, true as unknown as number]) {
    const { parent, calls } = fixture();
    await assert.rejects(forkArker(parent, async vm => vm, vm => vm.delete(), { count }), InvalidArgumentError);
    assert.equal(calls.length, 0);
  }
});

test("checkpoint cleanup failure also cleans children that cannot be returned", async () => {
  const { parent, checkpoint, calls } = fixture();
  checkpoint.delete = async () => { throw new Error("cleanup checkpoint"); };
  await assert.rejects(forkArker(parent, async vm => vm, vm => vm.delete(), { count: 2 }), AggregateError);
  assert.deepEqual(calls.filter(([name]) => name === "delete"), [["delete", 0], ["delete", 1]]);
});

test("abort after a child commits cleans it without additional fanout", async () => {
  const { parent, checkpoint, children, calls } = fixture();
  const controller = new AbortController();
  checkpoint.fork = async () => { controller.abort(); return children[0]; };
  await assert.rejects(forkArker(parent, async vm => vm, vm => vm.delete(), { count: 2, signal: controller.signal }));
  assert.deepEqual(calls.map(([name]) => name), ["capture", "delete", "checkpoint-delete"]);
});

test("exported facade keeps native fork ownership and per-child errors", async () => {
  const failed = new Error("native child");
  const child = { sandboxId: "native-child" };
  const opts = { count: 2, timeoutMs: 9000 };
  const native = { async fork(actual: unknown) { assert.equal(actual, opts); return [child, failed]; } };
  const box = { provider: "e2b", sandboxId: "native-parent", getInstance: () => native };
  const parent = new (Sandbox as any)(box, native) as Sandbox;
  const results = await parent.fork(opts);
  assert.equal((results[0] as Sandbox).sandboxId, "native-child");
  assert.equal(results[1], failed);
});

test("redacted managed ingress is reconstructed; custom ingress fails before capture", async () => {
  const a = fixture();
  const rule = a.parent.policies!.rules!["computesdk-e2b-apps"] as any;
  delete rule.auth;
  rule.match.headers["e2b-traffic-access-token"] = ["***"];
  await forkArker(a.parent, async vm => vm, vm => vm.delete());
  const child = a.calls.find(([name]) => name === "child")![1];
  assert.deepEqual(child.policies.rules["computesdk-e2b-apps"].match.headers, { "e2b-traffic-access-token": ["${secret:computesdk-e2b-traffic}"] });
  const b = fixture();
  b.parent.policies!.rules!.custom = { type: "inbound", priority: 10, action: "allow", match: { ports: [8080], headers: { secret: ["***"] } } };
  await assert.rejects(forkArker(b.parent, async vm => vm, vm => vm.delete()), /custom inbound/);
  assert.equal(b.calls.length, 0);
});

test("atomic capture failure prevents fanout", async () => {
  const { parent, calls } = fixture();
  parent.fork = async () => { throw new Error("capture refused"); };
  await assert.rejects(forkArker(parent, async vm => vm, vm => vm.delete()), /capture refused/);
  assert.deepEqual(calls, []);
});

test("checkpoint has a budget deadline and is deleted before readiness", async () => {
  const { parent, calls } = fixture();
  await forkArker(parent, async vm => {
    assert.equal(calls.filter(([name]) => name === "child").length, 2);
    assert.ok(calls.some(([name]) => name === "checkpoint-delete"));
    return vm;
  }, vm => vm.delete(), { count: 2, requestTimeoutMs: 7000 });
  const retention = calls[0][1].policies.rules["computesdk-e2b-lifetime"].after_created;
  assert.ok(retention > 0 && retention <= 7);
  await assert.rejects(forkArker(parent, async vm => vm, vm => vm.delete(), { requestTimeoutMs: 0 }), /finite requestTimeoutMs/);
});

test("concurrent inventory excludes the checkpoint and keeps child provenance", async t => {
  const { Arker } = await import("../src/index.js");
  const { listSandboxes } = await import("../src/compat/e2b-list.js");
  const { preserveEnvironment, preserveProperties } = await import("./provider-fixtures.js");
  preserveEnvironment(t, "ARKER_API_KEY", "E2B_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  preserveProperties(t, Arker.prototype, "listVms");
  process.env.ARKER_API_KEY = "test";
  delete process.env.E2B_API_KEY;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const { parent, checkpoint, children } = fixture();
  const rows: VM[] = [parent];
  let unblock!: () => void, entered!: () => void;
  const released = new Promise<void>(resolve => { unblock = resolve; });
  const blocked = new Promise<void>(resolve => { entered = resolve; });
  for (const row of [parent, checkpoint, ...children]) {
    Object.assign(row, { baseUrl: "https://regional.example.test", created_at: "2026-01-01T00:00:00Z",
      resources: { vcpu: 2, memory_mib: 512 }, refresh: async () => row,
      _client: { _request: async () => ({ vm_id: (row as VM).id, state: "idle", mode: null,
        customer_paused: false, started_at: "2026-01-01T00:00:00Z" }) } });
  }
  Object.assign(parent.policies!.rules!, { "computesdk-e2b-lifetime": { type: "schedule", after_created: 300, action: "delete" } });
  parent.fork = async (options = {}) => {
    Object.assign(checkpoint, { metadata: options.metadata, immutable: options.immutable });
    rows.push(checkpoint as unknown as VM);
    return checkpoint as unknown as VM;
  };
  let index = 0;
  checkpoint.fork = async (options: any) => {
    if (index === 1) { entered(); await released; }
    const child = children[index++];
    Object.assign(child, { metadata: options.metadata, policies: options.policies });
    rows.push(child);
    return child;
  };
  checkpoint.delete = async () => { rows.splice(rows.indexOf(checkpoint as unknown as VM), 1); };
  Arker.prototype.listVms = async options => {
    const filter = JSON.parse(options!.metadata_filter!);
    assert.deepEqual(filter, { arker_e2b: { version: 1 } });
    return { vms: rows.filter(row => (row.metadata?.arker_e2b as any)?.version === filter.arker_e2b.version), nextCursor: null };
  };
  const running = forkArker(parent, async vm => vm, vm => vm.delete(), { count: 2 });
  try {
    await blocked;
    assert.ok(rows.includes(checkpoint as unknown as VM));
    assert.deepEqual((checkpoint as any).metadata, { arker_e2b_checkpoint: { version: 1, source_vm_id: parent.id } });
    const visible = await listSandboxes().nextItems();
    assert.deepEqual(visible.map(row => row.sandboxId), [parent.id, children[0].id]);
    assert.ok(visible.every(row => row.templateId === "source" && row.metadata.team === "test"));
  } finally {
    unblock();
    await running;
  }
  assert.ok(!rows.includes(checkpoint as unknown as VM));
  assert.ok(children.every(child => JSON.stringify(child.metadata) === JSON.stringify(parent.metadata)));
});
