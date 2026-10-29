import assert from "node:assert/strict";
import test from "node:test";
import { Sandbox as NativeSandbox } from "e2b";
import { Arker, type VM } from "../src/index.js";
import { sandboxInfo } from "../src/compat/e2b-inventory.js";
import { listSandboxes } from "../src/compat/e2b-list.js";
import { preserveEnvironment, preserveProperties } from "./provider-fixtures.js";

function fixture(id = "sandbox") {
  const lifecycle = { vm_id: id, state: "running", mode: null as string | null, customer_paused: false,
    started_at: "2026-01-02T03:04:05Z", paused_at: null as string | null };
  const calls: string[] = [];
  const vm = { id, baseUrl: "https://example.invalid", created_at: "2026-01-01T00:00:00Z",
    metadata: { arker_e2b: { version: 1, metadata: { purpose: "test" }, source: { kind: "vm_name", value: "actual-source" }, envd_version: "0.10.2" } },
    policies: { rules: { "computesdk-e2b-lifetime": { type: "schedule", action: "delete", at: "2026-01-02T03:09:05Z" } } },
    resources: { vcpu: 2, memory_mib: 512 },
    refresh: async () => { calls.push("refresh"); return vm; },
    _client: { _request: async (method: string, path: string) => { assert.equal(method, "GET"); assert.ok(path.endsWith("/lifecycle")); calls.push("lifecycle"); return lifecycle; } },
  };
  return { vm: vm as unknown as VM, raw: vm, lifecycle, calls };
}

test("info maps observed resources, source, metadata and resumed timestamps without guest I/O", () => {
  const f = fixture();
  const result = sandboxInfo(f.vm, f.lifecycle);
  assert.equal(result.templateId, "actual-source"); assert.equal(result.name, "actual-source");
  assert.equal(result.startedAt.toISOString(), "2026-01-02T03:04:05.000Z");
  assert.equal(result.endAt.toISOString(), "2026-01-02T03:09:05.000Z");
  assert.equal(result.cpuCount, 2); assert.equal(result.memoryMB, 512);
  assert.deepEqual(result.metadata, { purpose: "test" });
  f.lifecycle.state = "paused"; f.lifecycle.customer_paused = true; f.lifecycle.mode = "full";
  f.lifecycle.paused_at = "2026-01-02T03:06:00Z";
  assert.equal(sandboxInfo(f.vm, f.lifecycle).state, "paused");
  assert.equal(sandboxInfo(f.vm, f.lifecycle).endAt.toISOString(), "2026-01-02T03:06:00.000Z");
  assert.deepEqual(f.calls, []);
});

test("missing resource, provenance or timestamps never acquire invented defaults", () => {
  for (const field of ["started_at"] as const) {
    const f = fixture();
    assert.throws(() => sandboxInfo(f.vm, { ...f.lifecycle, [field]: null }), /does not expose/);
  }
  for (const resource of ["vcpu", "memory_mib"] as const) {
    const f = fixture();
    (f.raw.resources as Record<string, unknown>)[resource] = null;
    assert.throws(() => sandboxInfo(f.vm, f.lifecycle), /concrete CPU and memory/);
  }
  const f = fixture(); f.raw.metadata.arker_e2b.version = 2;
  assert.throws(() => sandboxInfo(f.vm, f.lifecycle), /descriptor/);
});

test("Arker paginator forwards metadata filters and opaque cursors without changing state", async t => {
  preserveEnvironment(t, "ARKER_API_KEY", "E2B_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  preserveProperties(t, Arker.prototype, "listVms");
  process.env.ARKER_API_KEY = "test"; delete process.env.E2B_API_KEY; delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const f = fixture(); const requests: unknown[] = [];
  Arker.prototype.listVms = async options => { requests.push(options); return { vms: [f.vm], nextCursor: requests.length === 1 ? "opaque-next" : null }; };
  const pages = listSandboxes({ query: { metadata: { purpose: "test" }, state: ["running"] }, limit: 2 });
  assert.equal(pages.hasNext, true);
  assert.equal((await pages.nextItems())[0].sandboxId, "sandbox");
  assert.equal(pages.nextToken, "opaque-next");
  await pages.nextItems(); assert.equal(pages.hasNext, false); assert.equal(pages.nextToken, undefined);
  assert.deepEqual(requests, [undefined, "opaque-next"].map(cursor => ({ cursor, limit: 2,
    metadata_filter: JSON.stringify({ arker_e2b: { version: 1, metadata: { purpose: "test" } } }),
  })));
  await assert.rejects(pages.nextItems(), /No more/);
  assert.ok(f.calls.every(call => call === "refresh" || call === "lifecycle"));
});

test("native paginator keeps object identity and receives original options", t => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  preserveProperties(t, NativeSandbox, "list");
  delete process.env.ARKER_API_KEY; delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const options = { order: "asc" as const, query: { startedAfter: new Date(0) }, requestTimeoutMs: 23 };
  const pages = NativeSandbox.list();
  NativeSandbox.list = actual => { assert.deepEqual(actual, options); return pages; };
  assert.equal(listSandboxes(options), pages);
});


for (const open of [true, false]) test(`inventory exposes native public traffic flag ${open} from actual ingress`, () => {
  const f = fixture();
  Object.assign(f.raw.policies.rules, {
    apps: { type: "inbound", priority: 0, action: "allow", auth: open ? "open" : "arker", match: { ports: [[1, 49982], [49984, 65535]] } },
    control: { type: "inbound", priority: 1, action: "allow", auth: "arker", match: { ports: [49983] } },
  });
  assert.deepEqual(sandboxInfo(f.vm, f.lifecycle).network, { allowPublicTraffic: open });
  Object.assign(f.raw.policies.rules, { custom: { type: "inbound", action: "deny", match: { ports: [8080] } } });
  assert.equal(sandboxInfo(f.vm, f.lifecycle).network, undefined);
});


test("paused expiration requires the persisted pause clock, never the removed timer", () => {
  const f = fixture();
  f.lifecycle.state = "paused"; f.lifecycle.customer_paused = true; f.lifecycle.mode = "full";
  assert.throws(() => sandboxInfo(f.vm, f.lifecycle), /pause timestamp/);
  f.lifecycle.paused_at = "2026-01-02T03:06:00Z";
  delete (f.raw.policies.rules as Record<string, unknown>)["computesdk-e2b-lifetime"];
  assert.equal(sandboxInfo(f.vm, f.lifecycle).endAt.toISOString(), "2026-01-02T03:06:00.000Z");
  f.lifecycle.state = "running"; f.lifecycle.customer_paused = false;
  assert.throws(() => sandboxInfo(f.vm, f.lifecycle), /lifetime delete schedule/);
});
