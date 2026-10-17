import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AlreadyExistsError, InvalidError } from "modal";

import { createRoutedProvider, JsonRouteCache, parseRouterConfig, isArkerVmIdentifier, providerAcceptsIdentifier } from "../src/compat/router.js";

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

    Math.random = () => 0.25;
    const largeWeightsPath = await config(directory, {
      version: 1,
      existing: { strategy: "priority", order: ["blaxel", "arker"] },
      create: { strategy: "weighted", weights: { blaxel: 1e308, arker: 1e308 } },
    });
    const largeWeights = createRoutedProvider([provider("blaxel"), provider("arker")], largeWeightsPath).instances!;
    assert.equal((await largeWeights.create()).provider, "blaxel");
    Math.random = () => 0.75;
    assert.equal((await largeWeights.create()).provider, "arker");

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
    let wrapped: Error = new Error("unauthorized");
    for (let index = 0; index < 8; index++) wrapped = new Error("provider creation failed", { cause: wrapped });
    const cyclic = new Error("cyclic provider error");
    cyclic.cause = cyclic;
    for (const error of [
      wrapped,
      cyclic,
      new TypeError("fetch failed"),
      Object.assign(new Error("read failed"), { code: "ECONNRESET" }),
      Object.assign(new Error("write failed"), { code: "EPIPE" }),
      new Error("socket hang up"),
      { code: "SANDBOX_ALREADY_EXISTS", message: "exists" },
      Object.assign(new Error("conflict"), { status: 409 }),
      Object.assign(new Error("opaque"), { status: 503 }),
      new AlreadyExistsError("opaque"),
      new InvalidError("opaque"),
      Object.assign(new Error("opaque"), { code: 14, details: "opaque", path: "/modal.client.ModalClient/SandboxCreate" }),
    ]) {
      const reset = createRoutedProvider([
        provider("blaxel", { create: async () => { throw error; } }),
        provider("arker", { create: async () => { duplicateCreates += 1; return sandbox("duplicate", "arker"); } }),
      ]).instances!;
      await assert.rejects(reset.create(), (caught: unknown) => caught === error);
    }
    assert.equal(duplicateCreates, 0, "transport failures do not prove the first creation failed");

    const created = { get instanceId(): string { throw new Error("metadata unavailable"); } };
    const metadataFailure = createRoutedProvider([
      provider("blaxel", { create: async () => created }),
      provider("arker", { create: async () => { throw new Error("must not create another sandbox"); } }),
    ]).instances!;
    assert.equal(await metadataFailure.create(), created);
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

  const sameClock = new JsonRouteCache(join(directory, "same-clock.json"), 2, 4096, () => 0);
  await sameClock.set("old", "arker");
  await sameClock.set("cold", "blaxel");
  assert.equal(await sameClock.get("old"), "arker");
  await sameClock.set("new", "arker");
  assert.equal(await sameClock.get("cold"), null);
  assert.equal(await sameClock.get("old"), "arker");
}

async function testMemoryCacheIsBoundedAndPromotesReads(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-memory-cache-"));
  const path = await config(directory, {
    version: 1,
    existing: { strategy: "priority", order: ["arker", "blaxel"] },
    create: { strategy: "priority", order: ["blaxel"] },
    cache: { maxEntries: 2, maxBytes: 4096 },
  });
  const ids = ["old", "cold", "new"];
  let misses = 0;
  const routed = createRoutedProvider([
    provider("arker", { getById: async () => { misses += 1; return null; } }),
    provider("blaxel", {
      create: async () => sandbox(ids.shift()!, "blaxel"),
      getById: async (id) => sandbox(id, "blaxel"),
    }),
  ], path).instances!;
  await routed.create();
  await routed.create();
  await routed.getById("old");
  assert.equal(misses, 0);
  await routed.create();
  await routed.getById("cold");
  assert.equal(misses, 1, "least-recently-used ownership must be evicted without an unbounded shadow map");
}

async function testDestroyRevalidatesStaleOwnership(): Promise<void> {
  let destroyed = "";
  const routed = createRoutedProvider([
    provider("blaxel", {
      create: async () => sandbox("moved", "blaxel"),
      getById: async () => null,
      destroy: async () => { destroyed = "wrong-provider"; },
    }),
    provider("arker", {
      getById: async (id) => sandbox(id, "arker"),
      destroy: async () => { destroyed = "arker"; },
    }),
  ]).instances!;
  await routed.create();
  await routed.destroy("moved");
  assert.equal(destroyed, "arker");
}

await testPriorityAndRaceRouting();
await testWeightedFallbackAndNonRetryableErrors();
await testPersistentCacheAndDestroyOwnership();
await testConfigAndCacheValidation();
await testMemoryCacheIsBoundedAndPromotesReads();
await testDestroyRevalidatesStaleOwnership();

async function testCacheCannotOverrideConfiguredProviders(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-disabled-"));
  const cachePath = join(directory, "ownership.json");
  await new JsonRouteCache(cachePath).set("existing", "arker");
  const path = await config(directory, {
    version: 1,
    existing: { strategy: "priority", order: ["blaxel"] },
    create: { strategy: "priority", order: ["blaxel"] },
    cache: { path: cachePath },
  });
  const routed = createRoutedProvider([
    provider("arker", { getById: async () => { throw new Error("excluded provider queried"); } }),
    provider("blaxel", { getById: async (id) => sandbox(id, "blaxel") }),
  ], path).instances!;
  assert.equal((await routed.getById("existing"))?.provider, "blaxel");
}

await testCacheCannotOverrideConfiguredProviders();

async function testNativeFirstArkerIdentifiers(): Promise<void> {
  const cases: Array<{ id: string; arker: boolean }> = JSON.parse(await readFile(new URL("../../compatibility/router-identifiers.json", import.meta.url), "utf8"));
  for (const testCase of cases) {
    assert.equal(isArkerVmIdentifier(testCase.id), testCase.arker);
    for (const name of ["e2b", "modal", "blaxel", "daytona", "arker"]) {
      assert.equal(providerAcceptsIdentifier(name, testCase.id, false), true);
      assert.equal(providerAcceptsIdentifier(name, testCase.id, true), !testCase.arker || !["e2b", "modal"].includes(name));
    }
  }
  const id = "vmh-b471cad989f2eab9-01M548DGAXJ48SGE3SRTRR8HNV";
  for (const native of ["e2b", "modal"]) {
    for (const strategy of ["priority", "race"]) {
      for (const staleCache of [false, true]) {
        const directory = await mkdtemp(join(tmpdir(), "arker-id-routing-"));
        const cachePath = join(directory, "cache.json");
        if (staleCache) await new JsonRouteCache(cachePath).set(id, native);
        const path = await config(directory, {
          version: 1, existing: { strategy, order: [native, "arker"] },
          create: { strategy: "priority", order: ["arker"] },
          cache: { path: cachePath },
        });
        const routed = createRoutedProvider([
          provider(native, { getById: async () => { throw new Error("native ID format is incompatible"); } }),
          provider("arker", { getById: async () => sandbox(id, "arker") }),
        ], path).instances!;
        assert.equal((await routed.getById(id))?.provider, "arker");
      }
    }
  }
}
await testNativeFirstArkerIdentifiers();

async function testOwnershipProbesDoNotConnectLosingSandboxes(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-probe-routing-"));
  const path = await config(directory, {
    version: 1, existing: { strategy: "race", order: ["e2b", "arker"] },
    create: { strategy: "priority", order: ["arker"] },
  });
  let connects = 0;
  let release!: (found: boolean) => void;
  const native = Object.assign(provider("e2b", {
    getById: async (id) => { connects++; return sandbox(id, "e2b"); },
  }), { ownershipProbe: () => new Promise<boolean>((resolve) => { release = resolve; }) });
  const routed = createRoutedProvider([
    native, provider("arker", { getById: async (id) => sandbox(id, "arker") }),
  ], path).instances!;
  assert.equal((await routed.getById("shared"))?.provider, "arker");
  release(true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(connects, 0);

  let probes = 0;
  let deleted = 0;
  const nativeOnly = createRoutedProvider([Object.assign(provider("e2b", {
    getById: async (id) => { connects++; return sandbox(id, "e2b"); },
    destroy: async () => { deleted++; },
  }), { ownershipProbe: async () => { probes++; return true; } })]).instances!;
  assert.equal((await nativeOnly.getById("native"))?.provider, "e2b");
  assert.equal(connects, 1);
  await nativeOnly.destroy("native");
  assert.equal(probes, 2);
  assert.equal(connects, 1);
  assert.equal(deleted, 1);
}
await testOwnershipProbesDoNotConnectLosingSandboxes();

console.log("PASS compat router");
