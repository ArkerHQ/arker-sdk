import assert from "node:assert/strict";
import test from "node:test";
import type { VM } from "../src/index.js";
import { configureE2BIngress, validateE2BNetwork } from "../src/compat/e2b-network.js";

for (const publicTraffic of [true, false]) {
  test(`default ingress public=${publicTraffic} excludes the control port`, async () => {
    let change: any;
    const vm = { refresh: async () => ({ policies: { rules: { outbound: { type: "outbound", action: "deny" } }, secrets: { retained: "***" } } }), update: async (value: unknown) => { change = value; } };
    await configureE2BIngress(vm as unknown as VM, publicTraffic);
    const rules = change.policies.rules;
    const app = Object.values(rules).find((rule: any) => rule.match.ports.length === 2) as any;
    const control = Object.values(rules).find((rule: any) => rule.match.ports.length === 1) as any;
    assert.deepEqual(app.match.ports, [[1, 49982], [49984, 65535]]);
    assert.equal(app.auth, publicTraffic ? "open" : "arker");
    assert.deepEqual(control.match.ports, [49983]);
    assert.equal(control.auth, "arker");
    assert.deepEqual(Object.keys(change.policies), ["rules"]);
  });
}

test("custom inbound policy is preserved; explicit private conflicts fail before mutation", async () => {
  const vm = { refresh: async () => ({ policies: { rules: { custom: { type: "inbound", action: "allow", auth: "open", match: { ports: [8080] } } } } }), update: async () => { throw new Error("must not mutate custom policy"); } };
  await configureE2BIngress(vm as unknown as VM, true);
  await assert.rejects(configureE2BIngress(vm as unknown as VM, false), /conflicts/);
});

test("network options reject unsupported shapes and values", () => {
  for (const value of [null, [], { allowPublicTraffic: "false" }, { unknown: true }]) assert.throws(() => validateE2BNetwork(value as never), TypeError);
  for (const value of [undefined, {}, { allowPublicTraffic: false }, { allowPublicTraffic: true }]) validateE2BNetwork(value);
});
