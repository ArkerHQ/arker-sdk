import { readFile } from "node:fs/promises";
import { test } from "node:test";
import assert from "node:assert/strict";
import { creationKey, outboundPolicy, creationIntent, resolveNetwork } from "../src/compat/e2b-create.js";

test("E2B keyed creation and DNS policy contract", () => {
assert.equal(creationKey("a".repeat(255)), "a".repeat(255));
for (const invalid of ["", "a".repeat(256), "UPPER", "a/b", 123]) assert.throws(() => creationKey(invalid));
const network = { denyOut: ["0.0.0.0/0"], allowOut: ["example.com"], dnsServers: ["1.1.1.1"] };
const saved = structuredClone(network);
const policy = outboundPolicy(network)!;
assert.deepEqual(network, saved);
assert.deepEqual(Object.values(policy.rules), [{type: "outbound_guard", allow: [{hosts: ["example.com"]}, {ips: ["1.1.1.1"]}]}, {type: "outbound_guard", allow: [{ports: [[1,52],[54,65535]]}, {ips: ["1.1.1.1"]}]}]);
assert.deepEqual(outboundPolicy(network), policy);
assert.equal(outboundPolicy({dnsServers: []}), undefined);
assert.notDeepEqual(outboundPolicy({...network, dnsServers: []}), policy);
for (const dnsServers of [["::1"], ["not-an-ip"], Array(11).fill("1.1.1.1")]) assert.throws(() => outboundPolicy({dnsServers}));
assert.equal(creationIntent({templateId: "template", envs: {VALUE: "one"}}), creationIntent({envs: {VALUE: "one"}, templateId: "template"}));
assert.notEqual(creationIntent({envs: {VALUE: "one"}}), creationIntent({envs: {VALUE: "two"}}));
});

test("client defaults and callback identity do not change creation intent", () => {
  assert.equal(creationIntent({templateId: "template"}), creationIntent({templateId: "template",
    secure: false, validateApiKey: false, debug: false, requestTimeoutMs: 0,
    envs: {}, metadata: {}, lifecycle: {onTimeout: "kill"}, volumeMounts: {},
  }));
  const network = resolveNetwork({allowOut: ({rules}: {rules: Map<string, unknown>}) => [...rules.keys()]});
  assert.deepEqual(network, {allowOut: []});
  assert.equal(creationIntent({network}), creationIntent({network: {allowOut: []}}));
  assert.equal(creationIntent({timeoutMs: 300001}), creationIntent({timeout: 300001}));
});


test("native network defaults do not turn allow-only IP selectors into restrictions", () => {
  assert.equal(outboundPolicy({allowOut: []}), undefined);
  assert.equal(outboundPolicy({allowOut: ["8.8.8.8"]}), undefined);
  assert.throws(() => outboundPolicy({allowOut: ["example.com"]}), /require denyOut/);
  const blocked = outboundPolicy({denyOut: ["0.0.0.0/0"], dnsServers: ["1.1.1.1"]})!;
  assert.deepEqual(Object.values(blocked.rules)[0]!.allow, []);
});

test("creation intent matches shared language vectors including Unicode metadata keys", async () => {
  const cases = JSON.parse(await readFile("../compatibility/e2b-create-vectors.json", "utf8"));
  for (const entry of cases) {
    const { network, ...options } = entry.options;
    const convertedNetwork = network && Object.fromEntries(Object.entries(network).map(([key, value]) =>
      [key.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase()), value]));
    assert.equal(creationIntent({ ...options, templateId: entry.template, timeoutMs: entry.timeout * 1000,
      allowInternetAccess: entry.internet, ...(network ? {network: convertedNetwork} : {}) }), entry.hash);
  }
});


test("DNS IP/port floor cannot inherit a hostname resolver exception", () => {
  for (const dnsServers of [[], ["1.1.1.1"], ["8.8.8.8", "1.1.1.1"]]) {
    const policy = outboundPolicy({allowOut: ["dns.google", "9.9.9.0/24"], denyOut: ["0.0.0.0/0"], dnsServers})!;
    assert.ok(Object.keys(policy.rules).every(name => name.length <= 64));
    const floor = Object.values(policy.rules)[1]!;
    assert.deepEqual(floor.allow, [{ports: [[1,52],[54,65535]]}, {ips: ["9.9.9.0/24", ...dnsServers]}]);
    assert.ok(floor.allow.every(scope => !("hosts" in scope)));
  }
});
