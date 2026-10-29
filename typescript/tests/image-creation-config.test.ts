import assert from "node:assert/strict";
import { test } from "node:test";
import { blaxelImageConfiguration } from "../src/compat/blaxel-create.js";

test("creation environment preserves inherited values and explicit startup clears", () => {
  const imageConfig = { env: { BASE: "inherited", HOME: "/old" }, entrypoint: [], cmd: [] };
  const result = blaxelImageConfiguration({ image: "example/image", envs: { HOME: "/new", VALUE: "'$(literal)", EMPTY: "" } }, { imageConfig });
  assert.deepEqual(result, { env: { BASE: "inherited", HOME: "/new", VALUE: "'$(literal)", EMPTY: "" }, entrypoint: [], cmd: [] });
  assert.equal(imageConfig.env.HOME, "/old");
});

test("creation environment rejects malformed entries and VM sources", () => {
  assert.throws(() => blaxelImageConfiguration({ envs: { X: "value" } }), /image source/);
  for (const envs of [[], null, false, { X: 1 }, { "BAD=KEY": "value" }, { X: "\0" }]) {
    assert.throws(() => blaxelImageConfiguration({ image: "example/image", envs }), /environment/);
  }
});

test("empty environment preserves config defaults and prototype names remain data", () => {
  assert.equal(blaxelImageConfiguration({ envs: {} }), undefined);
  const result = blaxelImageConfiguration({ image: "example/image", envs: JSON.parse('{"__proto__":"literal"}') });
  assert.equal(result?.env?.["__proto__"], "literal");
  assert.equal(Object.getPrototypeOf(result?.env), Object.prototype);
});


test("Blaxel facade sends creation environment in the preboot fork contract", async t => {
  const { mkdtemp, writeFile, rm } = await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { Arker } = await import("../src/index.js");
  const { SandboxInstance } = await import("../src/blaxel.js");
  const { preserveProperties } = await import("./provider-fixtures.js");
  const dir = await mkdtemp(join(tmpdir(), "creation-config-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const routerConfigPath = join(dir, "router.json");
  await writeFile(routerConfigPath, JSON.stringify({version: 1, create: {strategy: "priority", order: ["arker"]}, existing: {strategy: "priority", order: ["arker"]}}));
  preserveProperties(t, Arker.prototype, "_request");
  const stop = new Error("transport reached");
  const bodies: unknown[] = [];
  Arker.prototype._request = async (_method, _path, body) => { bodies.push(body); throw stop; };
  await assert.rejects(SandboxInstance.create({image: "example/image", envs: {HOME: "/workspace", EMPTY: ""}}, {
    routerConfigPath,
    arker: {apiKey: "ark_test", baseUrl: "https://custom.example/api", imageConfig: {cmd: [], entrypoint: [], env: {BASE: "inherited"}}},
  }), error => error === stop);
  assert.equal(bodies.length, 1);
  assert.deepEqual((bodies[0] as {image_config: unknown}).image_config, {
    cmd: [], entrypoint: [], env: {BASE: "inherited", HOME: "/workspace", EMPTY: ""},
  });
});


test("creation envs never implicitly suppress customer startup", () => {
  const values = {image: "example/image", envs: {X: "value"}};
  assert.deepEqual(blaxelImageConfiguration(values), {env: {X: "value"}});
  const imageConfig = {entrypoint: ["/application"], cmd: ["--serve"]};
  assert.deepEqual(blaxelImageConfiguration(values, {imageConfig}), {...imageConfig, env: {X: "value"}});
});
