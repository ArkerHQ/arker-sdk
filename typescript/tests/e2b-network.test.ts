import assert from "node:assert/strict";
import test from "node:test";
import { trafficPolicies, forkWithTraffic, trafficToken, validateE2BNetwork } from "../src/compat/e2b-network.js";
const APP = "computesdk-e2b-apps", CONTROL = "computesdk-e2b-envd", SECRET = "computesdk-e2b-traffic";

test("private token is app-only and generated in the fork request", async () => {
  const source = trafficPolicies(undefined, undefined, false);
  let request: any;
  const vm = { id: "child" };
  const client = { getVm: async () => ({ policies: source.policies }), fork: async (value: any) => { request = value; return vm; } };
  await forkWithTraffic(client as any, { source_vm_id: "parent" }, false);
  const token = trafficToken(vm as any);
  assert.match(token!, /^[A-Za-z0-9_-]{43}$/);
  assert.notEqual(token, source.token);
  assert.equal(request.policies.secrets[SECRET], token);
  assert.deepEqual(request.policies.rules[APP].match, { ports: [[1, 49982], [49984, 65535]], headers: { "e2b-traffic-access-token": [`\${secret:${SECRET}}`] } });
  assert.deepEqual(request.policies.rules[CONTROL].match.ports, [49983]);
  assert.equal(request.policies.rules[CONTROL].auth, "arker");
  assert.equal(request.policies.rules[APP].auth, "open");
});

test("public defaults omit token; unrelated secret/outbound updates preserved", () => {
  const configured: any = { secrets: { other: "synthetic" }, rules: { outbound: { type: "outbound", action: "deny" } } };
  const plan = trafficPolicies(undefined, configured, true);
  assert.equal(plan.token, null);
  assert.equal(plan.policies.secrets?.[SECRET], null);
  assert.equal(plan.policies.secrets?.other, "synthetic");
  assert.equal(plan.policies.rules?.outbound, configured.rules.outbound);
  assert.equal((plan.policies.rules?.[APP] as any).match.headers, undefined);
});

test("custom policies stay authoritative; private conflicts refuse before fork", async () => {
  for (const auth of ["arker", "open"]) {
    const custom: any = { rules: { customer: { type: "inbound", action: "allow", auth, match: { ports: [8080] } } } };
    assert.deepEqual(trafficPolicies(custom, undefined, true), { policies: {}, token: null });
    assert.throws(() => trafficPolicies(custom, undefined, false), /conflicts/);
  }
  const modified = trafficPolicies(undefined, undefined, false).policies as any;
  modified.rules[APP].match.paths = ["/restricted"];
  assert.throws(() => trafficPolicies(modified, undefined, true), /Custom and managed/);
  await assert.rejects(forkWithTraffic({ getVm: async () => ({ policies: modified }), fork: async () => { throw new Error("must not fork"); } } as any, { source_vm_id: "source" }, false), /conflicts/);
});

test("legacy managed ingress replaced atomically, not left as public bypass", () => {
  const source: any = { rules: { "e2b-app-ingress-00000000-0000-0000-0000-000000000000": { type: "inbound", action: "allow", auth: "open", match: { ports: [[1, 49982], [49984, 65535]] } } } };
  const plan = trafficPolicies(source, undefined, false);
  assert.equal(plan.policies.rules?.[Object.keys(source.rules)[0]], null);
  assert.ok(plan.token);
});

test("network shape and reserved names are validated", () => {
  for (const value of [null, [], { allowPublicTraffic: "false" }, { unknown: true }]) assert.throws(() => validateE2BNetwork(value as never), TypeError);
  for (const value of [undefined, {}, { allowPublicTraffic: false }, { allowPublicTraffic: true }]) validateE2BNetwork(value);
  assert.throws(() => trafficPolicies(undefined, { secrets: { [SECRET]: "supplied" } }, false), /reserved/);
});

test("facade property delegates native and reads the VM-bound app token", async () => {
  const { Sandbox } = await import("../src/e2b.js");
  const { rememberTrafficToken } = await import("../src/compat/e2b-network.js");
  const vm = { id: "owned" } as any;
  const box = { sandboxId: "owned", provider: "arker", getInstance: () => vm };
  const sandbox = new (Sandbox as any)(box);
  assert.equal(sandbox.trafficAccessToken, undefined);
  rememberTrafficToken(vm, "a".repeat(43));
  assert.equal(sandbox.trafficAccessToken, "a".repeat(43));
  assert.equal(new (Sandbox as any)({ provider: "e2b" }, { trafficAccessToken: "native-token" }).trafficAccessToken, "native-token");
});


test("custom private ingress rejects before fork without changing source or configured policies", async () => {
  for (const [auth, action] of [["arker", "allow"], ["open", "allow"], [undefined, "allow"], ["arker", "deny"]]) {
    for (const configured of [false, true]) {
      const custom: any = { rules: { customer: { type: "inbound", action, match: { ports: [8080] }, ...(auth === undefined ? {} : { auth }) } } };
      const original = structuredClone(custom);
      let forks = 0;
      const client = {
        getVm: async () => ({ policies: configured ? undefined : custom }),
        fork: async () => { forks++; return { id: "unexpected" }; },
      };
      await assert.rejects(forkWithTraffic(client as any, { source_vm_id: "parent", ...(configured ? { policies: custom } : {}) }, false), /custom inbound policies/);
      assert.equal(forks, 0);
      assert.deepEqual(custom, original);
    }
  }
});


test("canonical managed GET omits open auth, including private redacted header", async () => {
  for (const privateTraffic of [false, true]) {
    const source: any = { rules: {
      [APP]: { type: "inbound", action: "allow", priority: 0, match: { ports: [[1, 49982], [49984, 65535]], ...(privateTraffic ? { headers: { "e2b-traffic-access-token": ["***"] } } : {}) } },
      [CONTROL]: { type: "inbound", action: "allow", priority: 1000000, auth: "arker", match: { ports: [49983] } },
    }, secrets: { [SECRET]: "***" } };
    const original = structuredClone(source);
    const calls: any[] = [];
    const child = { id: "child" } as any;
    const client = { getVm: async () => ({ policies: source }), fork: async (request: any) => { calls.push(request); return child; } };
    assert.equal(await forkWithTraffic(client as any, { source_vm_id: "parent" }, false), child);
    const token = trafficToken(child);
    assert.match(token!, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(calls[0].policies.secrets[SECRET], token);
    assert.equal(calls[0].policies.rules[APP].auth, "open");
    assert.deepEqual(calls[0].policies.rules[APP].match.headers, { "e2b-traffic-access-token": [`\${secret:${SECRET}}`] });
    assert.equal(calls[0].policies.rules[CONTROL].auth, "arker");
    assert.deepEqual(source, original);
  }
});

test("missing auth default cannot admit custom rules or the control port", async () => {
  for (const change of ["name", "ports", "paths", "header", "auth_null", "control_auth"]) {
    const source: any = { rules: { [APP]: { type: "inbound", action: "allow", match: { ports: [[1, 49982], [49984, 65535]], headers: { "e2b-traffic-access-token": ["***"] } } } } };
    const rule = source.rules[APP];
    if (change === "name") { source.rules.customer = rule; delete source.rules[APP]; }
    else if (change === "ports") rule.match.ports = [8080];
    else if (change === "paths") rule.match.paths = ["/restricted"];
    else if (change === "header") rule.match.headers = { "customer-token": ["***"] };
    else if (change === "auth_null") rule.auth = null;
    else source.rules[CONTROL] = { type: "inbound", action: "allow", match: { ports: [49983] } };
    const original = structuredClone(source);
    let forks = 0;
    const client = { getVm: async () => ({ policies: source }), fork: async () => { forks++; } };
    await assert.rejects(forkWithTraffic(client as any, { source_vm_id: "parent" }, false), /custom inbound/);
    assert.equal(forks, 0);
    assert.deepEqual(source, original);
  }
});
