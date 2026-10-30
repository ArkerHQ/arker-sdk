import assert from "node:assert/strict";
import { SandboxInstance as Native, settings, initialize } from "@blaxel/core";
import { test } from "node:test";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Arker } from "../src/index.js";
import { SandboxInstance, type BlaxelCreateOptions } from "../src/blaxel.js";
import { validateBlaxelNetwork } from "../src/compat/blaxel-network.js";
import { preserveEnvironment, preserveProperties } from "./provider-fixtures.js";

test("native no-op network retains the complete customer policy and private daemon ingress", async t => {
  preserveEnvironment(t, "ARKER_BASE_URL", "ARKER_CONTROL_BASE_URL");
  delete process.env.ARKER_BASE_URL;
  delete process.env.ARKER_CONTROL_BASE_URL;
  const dir = await mkdtemp(join(tmpdir(), "blaxel-network-"));
  t.after(() => rm(dir, {recursive: true, force: true}));
  const routerConfigPath = join(dir, "router.json");
  await writeFile(routerConfigPath, JSON.stringify({version: 1, create: {strategy: "priority", order: ["arker"]}, existing: {strategy: "priority", order: ["arker"]}}));
  const policies = {rules: {
    floor: {type: "outbound_guard" as const, allow: []},
    daemon: {type: "inbound" as const, priority: 100, action: "allow" as const, auth: "arker" as const, match: {ports: [49983]}},
  }, secrets: {customer: "test-only"}};
  const original = structuredClone(policies);
  const stop = new Error("creation transport reached");
  const bodies: unknown[] = [];
  preserveProperties(t, Arker.prototype, "_request");
  Arker.prototype._request = async (_method, _path, body) => { bodies.push(body); throw stop; };
  const options = {routerConfigPath, arker: {apiKey: "ark_test", baseUrl: "https://custom.example/api", source: "source", policies}};
  for (const network of [undefined, {}, {firewall: {}}, {firewall: {rulesets: []}}, {firewall: {rulesets: ["default"]}}]) {
    const before = bodies.length;
    await assert.rejects(SandboxInstance.create({network}, options), error => error === stop);
    assert.equal(bodies.length, before + 1);
    assert.deepEqual((bodies.at(-1) as {policies: unknown}).policies, original);
    assert.deepEqual(policies, original);
  }
  for (const network of [
    {allowedDomains: []}, {forbiddenDomains: ["example.com"]},
    {proxy: {allowedDomains: ["example.com"], bypass: []}},
    {proxy: {routing: [{headers: {Authorization: "test-only"}}]}},
    {egress: {}}, {subnet: "default"},
    {firewall: {rulesets: ["proxy"]}}, {firewall: {rulesets: ["dedicated-ip"]}},
    {firewall: {rulesets: ["default", "unknown"]}},
  ]) {
    const before = bodies.length;
    await assert.rejects(SandboxInstance.create({network} as BlaxelCreateOptions, options), error => {
      assert.match(String(error), /network/);
      assert.doesNotMatch(String(error), /test-only/);
      return true;
    });
    assert.equal(bodies.length, before);
  }
});

test("invalid nested firewall values cannot be silently discarded", () => {
  for (const value of [false, [], "default", {firewall: null}, {firewall: {rulesets: null}},
    {firewall: {rulesets: "default"}}, {firewall: {allowedDomains: ["example.com"]}}]) {
    assert.throws(() => validateBlaxelNetwork(value), /network/);
  }
});

test("native route retains proxy configuration without Arker validation", async t => {
  const config = {...settings.config};
  t.after(() => initialize(config));
  const dir = await mkdtemp(join(tmpdir(), "blaxel-native-network-"));
  t.after(() => rm(dir, {recursive: true, force: true}));
  const routerConfigPath = join(dir, "router.json");
  await writeFile(routerConfigPath, JSON.stringify({version: 1, create: {strategy: "priority", order: ["blaxel"]}, existing: {strategy: "priority", order: ["blaxel"]}}));
  const network = {proxy: {allowedDomains: ["example.com"]}};
  const stop = new Error("native creation reached");
  preserveProperties(t, Native, "create");
  Native.create = async options => {
    assert.deepEqual((options as {network?: unknown}).network, network);
    throw stop;
  };
  await assert.rejects(SandboxInstance.create({network}, {routerConfigPath, apiKey: "test", workspace: "test",
    arker: {apiKey: "ark_test", baseUrl: "https://custom.example/api", source: "source"}}), error => error === stop);
});

test("mountDrives reaches native unchanged and is rejected before Arker mutation", async t => {
  const config = {...settings.config};
  t.after(() => initialize(config));
  const dir = await mkdtemp(join(tmpdir(), "blaxel-drive-options-"));
  t.after(() => rm(dir, {recursive: true, force: true}));
  const routerConfigPath = join(dir, "router.json");
  const mountDrives = [{driveName: "test-drive", mountPath: "/data"}];
  const stop = new Error("native reached");
  preserveProperties(t, Native, "create");
  preserveProperties(t, Arker.prototype, "_request");
  Native.create = async (_options, callOptions) => {
    assert.deepEqual(callOptions?.mountDrives, mountDrives);
    throw stop;
  };
  let mutations = 0;
  Arker.prototype._request = async () => { mutations++; throw new Error("unexpected mutation"); };
  for (const provider of ["blaxel", "arker"]) {
    await writeFile(routerConfigPath, JSON.stringify({version: 1, create: {strategy: "priority", order: [provider]}, existing: {strategy: "priority", order: [provider]}}));
    await assert.rejects(SandboxInstance.create({}, {routerConfigPath, mountDrives, apiKey: "test", workspace: "test",
      arker: {apiKey: "ark_test", baseUrl: "https://custom.example/api", source: "source"}}), error =>
      provider === "blaxel" ? error === stop : /mountDrives/.test(String(error)));
  }
  assert.equal(mutations, 0);
});
