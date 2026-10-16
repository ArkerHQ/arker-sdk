import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createRoutedProvider, JsonRouteCache, parseRouterConfig } from "../src/compat/router.js";

function sandbox(id: string, provider: string) {
  return { instanceId: id, sandboxId: id, provider, destroy: async () => undefined };
}

function provider(name: string, handlers: {
  create?: () => Promise<any>;
  getById?: (id: string) => Promise<any | null>;
  destroy?: (id: string) => Promise<void>;
} = {}) {
  return {
    name,
    instances: {
      create: handlers.create ?? (async () => sandbox(`${name}-new`, name)),
      getById: handlers.getById ?? (async () => null),
      destroy: handlers.destroy ?? (async () => undefined),
    },
  };
}

async function config(directory: string, value: unknown): Promise<string> {
  const path = join(directory, "computesdk_router.json");
  await writeFile(path, JSON.stringify(value));
  return path;
}

async function testPriorityAndRaceRouting(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-"));
  const path = await config(directory, {
    version: 1,
    existing: { strategy: "race", order: ["blaxel", "arker"] },
    create: { strategy: "priority", order: ["blaxel", "arker"] },
  });
  let nativeCreates = 0;
  let arkerCreates = 0;
  const native = provider("blaxel", {
    create: async () => { nativeCreates += 1; return sandbox("native-new", "blaxel"); },
    getById: async () => new Promise<null>(() => undefined),
  });
  const arker = provider("arker", {
    create: async () => { arkerCreates += 1; return sandbox("arker-new", "arker"); },
    getById: async (id) => sandbox(id, "arker"),
  });
  const routed = createRoutedProvider([arker, native], path).instances!;
  assert.equal((await routed.create()).provider, "blaxel");
  assert.equal(nativeCreates, 1);
  assert.equal(arkerCreates, 0);
  assert.equal((await routed.getById("existing"))?.provider, "arker");
}

async function testWeightedFallbackAndNonRetryableErrors(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-"));
  const originalRandom = Math.random;
  Math.random = () => 0.95;
  try {
    const path = await config(directory, {
      version: 1,
      existing: { strategy: "priority", order: ["blaxel", "arker"] },
      create: { strategy: "weighted", weights: { blaxel: 90, arker: 10 }, fallback: ["arker", "blaxel"] },
    });
    let fallbackCreates = 0;
    const routed = createRoutedProvider([
      provider("blaxel", { create: async () => { fallbackCreates += 1; return sandbox("native", "blaxel"); } }),
      provider("arker", { create: async () => { throw new Error("capacity unavailable"); } }),
    ], path).instances!;
    assert.equal((await routed.create()).provider, "blaxel");
    assert.equal(fallbackCreates, 1);

    const auth = createRoutedProvider([
      provider("blaxel", { create: async () => { throw Object.assign(new Error("invalid API key"), { status: 401 }); } }),
      provider("arker", { create: async () => { throw new Error("must not run"); } }),
    ]).instances!;
    await assert.rejects(auth.create(), /invalid API key/);

    let duplicateCreates = 0;
    const ambiguous = createRoutedProvider([
      provider("blaxel", { create: async () => { throw new Error("Network failure; operation outcome is unknown"); } }),
      provider("arker", { create: async () => { duplicateCreates += 1; return sandbox("duplicate", "arker"); } }),
    ]).instances!;
    await assert.rejects(ambiguous.create(), /outcome is unknown/);
    assert.equal(duplicateCreates, 0, "ambiguous creation outcomes must never fall through and create a duplicate");
  } finally {
    Math.random = originalRandom;
  }
}

async function testPersistentCacheAndDestroyOwnership(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-"));
  const cachePath = join(directory, "routes.json");
  const path = await config(directory, {
    version: 1,
    existing: { strategy: "priority", order: ["blaxel", "arker"] },
    create: { strategy: "priority", order: ["blaxel", "arker"] },
    cache: { path: cachePath, maxEntries: 10, maxBytes: 4096 },
  });
  let nativeLookups = 0;
  let arkerLookups = 0;
  let destroyed = "";
  const arker = provider("arker", {
    getById: async (id) => { arkerLookups += 1; return sandbox(id, "arker"); },
    destroy: async (id) => { destroyed = `arker:${id}`; },
  });
  const native = provider("blaxel", { getById: async () => { nativeLookups += 1; return null; } });
  assert.equal((await createRoutedProvider([arker, native], path).instances!.getById("vm-1"))?.provider, "arker");
  assert.equal(nativeLookups, 1);
  assert.equal(arkerLookups, 1);

  await createRoutedProvider([arker, native], path).instances!.destroy("vm-1");
  assert.equal(destroyed, "arker:vm-1");
  assert.equal(nativeLookups, 1, "cached ownership must avoid probing Blaxel");
  assert.equal(JSON.parse(await readFile(cachePath, "utf8")).entries["vm-1"], undefined);
}

async function testConfigAndCacheValidation(): Promise<void> {
  const base = {
    version: 1,
    existing: { strategy: "race", order: ["blaxel", "arker"] },
    create: { strategy: "priority", order: ["blaxel", "arker"] },
  };
  assert.throws(() => parseRouterConfig({ ...base, apiKey: "secret" }, ["blaxel", "arker"]), /unknown field apiKey/);
  assert.throws(
    () => parseRouterConfig({ ...base, create: { strategy: "priority", order: ["missing"] } }, ["blaxel", "arker"]),
    /unknown provider/,
  );

  const directory = await mkdtemp(join(tmpdir(), "arker-cache-"));
  const path = join(directory, "routes.json");
  let now = 0;
  const cache = new JsonRouteCache(path, 2, 180, () => ++now);
  await cache.set("old", "blaxel");
  await cache.set("hot", "arker");
  await cache.get("old");
  await cache.set("new", "arker");
  assert.equal(await cache.get("hot"), null);
  assert.equal(await cache.get("old"), "blaxel");
  assert.equal(await cache.get("new"), "arker");
}

await testPriorityAndRaceRouting();
await testWeightedFallbackAndNonRetryableErrors();
await testPersistentCacheAndDestroyOwnership();
await testConfigAndCacheValidation();

console.log("PASS compat router");
