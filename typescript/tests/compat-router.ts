import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AlreadyExistsError, InvalidError } from "modal";

import { createRoutedProvider, JsonRouteCache, parseRouterConfig, isArkerVmIdentifier, providerAcceptsIdentifier, RouterLookupTimeoutError } from "../src/compat/router.js";
import { CompatibilityCreationNotStartedError } from "../src/compat/common.js";

const configCases: {
  base: Record<string, unknown>;
  cases: Array<{ name: string; patch: Record<string, unknown>; error?: string }>;
} = JSON.parse(await readFile(new URL("../../compatibility/router-config-cases.json", import.meta.url), "utf8"));
for (const testCase of configCases.cases) {
  const parse = () => parseRouterConfig({ ...configCases.base, ...testCase.patch }, ["blaxel", "arker"]);
  if (testCase.error) assert.throws(parse, new RegExp(testCase.error), testCase.name);
  else assert.equal(parse().version, 1, testCase.name);
}

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

async function testRaceErrorsAndPriorityAreDeterministic(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-errors-"));
  const path = join(directory, "router.json");
  await writeFile(path, JSON.stringify({
    version: 1,
    existing: { strategy: "race", order: ["blaxel", "arker"] },
    create: { strategy: "priority", order: ["blaxel", "arker"] },
  }));
  for (const status of [403, 429, 500]) {
    for (const errorFirst of [true, false]) {
      const error = Object.assign(new Error(`lookup HTTP ${status}`), { status });
      let release!: () => void;
      const firstFinished = new Promise<void>((resolve) => { release = resolve; });
      const routed = createRoutedProvider([
        provider("blaxel", { getById: async () => {
          if (!errorFirst) await firstFinished;
          release();
          throw error;
        } }),
        provider("arker", { getById: async (id) => {
          if (errorFirst) await firstFinished;
          release();
          return sandbox(id, "arker");
        } }),
      ], path).instances!;
      await assert.rejects(routed.getById("shared"), (caught) => caught === error);
    }
  }
  let release!: () => void;
  const lowerFinished = new Promise<void>((resolve) => { release = resolve; });
  const routed = createRoutedProvider([
    provider("blaxel", { getById: async (id) => { await lowerFinished; return sandbox(id, "blaxel"); } }),
    provider("arker", { getById: async (id) => { release(); return sandbox(id, "arker"); } }),
  ], path).instances!;
  assert.equal((await routed.getById("shared"))?.provider, "blaxel");
  const preferredError = Object.assign(new Error("preferred provider denied access"), { status: 403 });
  let releaseError!: () => void;
  const lowerError = new Promise<void>((resolve) => { releaseError = resolve; });
  const errors = createRoutedProvider([
    provider("blaxel", { getById: async () => { await lowerError; throw preferredError; } }),
    provider("arker", { getById: async () => { releaseError(); throw new Error("lower unavailable"); } }),
  ], path).instances!;
  await assert.rejects(errors.getById("shared"), (caught) => caught === preferredError);
}
await testRaceErrorsAndPriorityAreDeterministic();

async function testLookupDeadlineNeverCachesLateSuccess(): Promise<void> {
  for (const strategy of ["priority", "race"]) {
    const directory = await mkdtemp(join(tmpdir(), "arker-router-deadline-"));
    const cachePath = join(directory, "cache.json");
    const raw = {
      version: 1, existing: { strategy, order: ["blaxel", "arker"], timeoutMs: 20 },
      create: { strategy: "priority", order: ["blaxel", "arker"] }, cache: { path: cachePath },
    };
    const path = await config(directory, raw);
    let release!: () => void;
    const delayed = new Promise<void>((resolve) => { release = resolve; });
    const routed = createRoutedProvider([
      provider("blaxel", { getById: async (id) => { await delayed; return sandbox(id, "blaxel"); } }),
      provider("arker"),
    ], path).instances!;
    try {
      await assert.rejects(routed.getById("late"), RouterLookupTimeoutError);
    } finally { release(); }
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(await new JsonRouteCache(cachePath).get("late"), null);
    for (const timeoutMs of [true, 0, -1, 0.5, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
      assert.throws(() => parseRouterConfig({ ...raw, existing: { ...raw.existing, timeoutMs } }, ["blaxel", "arker"]), /positive safe integer/);
    }
  }
}
await testLookupDeadlineNeverCachesLateSuccess();

async function testCachedProbeSharesLookupBudget(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-budget-"));
  const cachePath = join(directory, "cache.json");
  await new JsonRouteCache(cachePath).set("shared", "blaxel");
  const path = await config(directory, {
    version: 1, existing: { strategy: "priority", order: ["blaxel", "arker"], timeoutMs: 30 },
    create: { strategy: "priority", order: ["blaxel", "arker"] }, cache: { path: cachePath },
  });
  const pause = () => new Promise<void>((resolve) => setTimeout(resolve, 20));
  const routed = createRoutedProvider([
    provider("blaxel", { getById: async () => { await pause(); return null; } }),
    provider("arker", { getById: async (id) => { await pause(); return sandbox(id, "arker"); } }),
  ], path).instances!;
  await assert.rejects(routed.getById("shared"), RouterLookupTimeoutError);
  await pause();
  assert.equal(await new JsonRouteCache(cachePath).get("shared"), null);
}
await testCachedProbeSharesLookupBudget();

async function testTimedOutProbeObservesLateRejection(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-late-error-"));
  const path = await config(directory, {
    version: 1, existing: { strategy: "race", order: ["blaxel", "arker"], timeoutMs: 10 },
    create: { strategy: "priority", order: ["blaxel", "arker"] },
  });
  let rejectNative!: (error: Error) => void;
  const pending = new Promise<null>((_resolve, reject) => { rejectNative = reject; });
  const unhandled: unknown[] = [];
  const listener = (error: unknown) => { unhandled.push(error); };
  process.on("unhandledRejection", listener);
  try {
    const routed = createRoutedProvider([
      provider("blaxel", { getById: () => pending }), provider("arker"),
    ], path).instances!;
    await assert.rejects(routed.getById("shared"), RouterLookupTimeoutError);
    rejectNative(new Error("late native read failure"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(unhandled, []);
  } finally { process.off("unhandledRejection", listener); }
}
await testTimedOutProbeObservesLateRejection();

async function testLateErrorCannotBeatElapsedDeadline(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-clock-"));
  const path = await config(directory, {
    version: 1, existing: { strategy: "priority", order: ["blaxel"], timeoutMs: 30 },
    create: { strategy: "priority", order: ["blaxel"] },
  });
  const descriptor = Object.getOwnPropertyDescriptor(performance, "now");
  let clock = 0;
  Object.defineProperty(performance, "now", { configurable: true, value: () => clock });
  try {
    const routed = createRoutedProvider([provider("blaxel", { getById: async () => {
      clock = 31;
      throw Object.assign(new Error("late forbidden response"), { status: 403 });
    } })], path).instances!;
    await assert.rejects(routed.getById("shared"), RouterLookupTimeoutError);
  } finally {
    if (descriptor) Object.defineProperty(performance, "now", descriptor);
    else Reflect.deleteProperty(performance, "now");
  }
}
await testLateErrorCannotBeatElapsedDeadline();

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
    getById: async () => null,
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
      provider("arker", { create: async () => { throw new CompatibilityCreationNotStartedError("local source is not configured"); } }),
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
      new Error("opaque provider failure"),
      new Error("opaque wrapper", { cause: new Error("opaque provider failure") }),
      Object.assign(new Error("opaque"), { status: 429 }),
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

  const invalidRoots = [null, [], true, 1, "cache", {
    version: true, entries: { old: { provider: "e2b", accessedAt: 1 } },
  }];
  for (const payload of invalidRoots) {
    await writeFile(path, JSON.stringify(payload));
    const recovering = new JsonRouteCache(path, 2, 4096, () => 100);
    assert.equal(await recovering.get("old"), null);
    await recovering.set("new", "arker");
    assert.equal(await recovering.get("new"), "arker");
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), {
      version: 1, entries: { new: { provider: "arker", accessedAt: 101 } },
    });
  }
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
  await new JsonRouteCache(cachePath).set("missing", "arker");
  const absent = createRoutedProvider([provider("blaxel"), provider("arker")], path).instances!;
  assert.equal(await absent.getById("missing"), null);
  assert.equal(await new JsonRouteCache(cachePath).get("missing"), null);
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
    native, provider("arker", { getById: async (id) => { release(false); return sandbox(id, "arker"); } }),
  ], path).instances!;
  assert.equal((await routed.getById("shared"))?.provider, "arker");
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


async function testCacheKeysAndByteBounds(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-keys-"));
  const path = join(directory, "cache.json");
  for (const id of ["__proto__", "constructor", "toString", "é".repeat(10)]) {
    const cache = new JsonRouteCache(path, 10, 100, () => 0);
    assert.equal(await cache.get(id), null);
    await cache.set(id, "arker");
    assert.equal(await cache.get(id), "arker");
    assert.ok((await readFile(path)).byteLength <= 100);
    await cache.delete(id);
    assert.equal(await cache.get(id), null);
  }
  const tiny = new JsonRouteCache(path, 10, 1);
  await tiny.set("id", "arker");
  assert.ok((await readFile(path).catch(() => Buffer.alloc(0))).byteLength <= 1);
  assert.equal(await tiny.get("id"), null);
  await writeFile(path, JSON.stringify({ version: 1, entries: [{ provider: "arker", accessedAt: 0 }] }));
  assert.equal(await new JsonRouteCache(path).get("0"), null);

  const toml = join(directory, "router.toml");
  await writeFile(toml, 'version = 1\n[existing]\nstrategy = "priority"\norder = ["arker"]\n');
  await assert.rejects(createRoutedProvider([provider("arker")], toml).instances!.create(), SyntaxError);

  const weights = JSON.parse('{"__proto__":1}');
  assert.deepEqual(parseRouterConfig({
    version: 1, existing: { strategy: "priority", order: ["__proto__"] },
    create: { strategy: "weighted", weights },
  }, ["__proto__"]).create, { strategy: "weighted", weights, fallback: ["__proto__"] });
}
await testCacheKeysAndByteBounds();

async function testCachedMissInvalidatesButErrorPreservesHint(): Promise<void> {
  for (const strategy of ["priority", "race"]) {
    const directory = await mkdtemp(join(tmpdir(), "arker-router-invalidation-"));
    const cachePath = join(directory, "cache.json");
    const cache = new JsonRouteCache(cachePath);
    await cache.set("shared", "blaxel");
    const path = await config(directory, {
      version: 1, existing: { strategy, order: ["blaxel", "arker"] },
      create: { strategy: "priority", order: ["blaxel"] }, cache: { path: cachePath },
    });
    const error = new Error("lookup unavailable");
    const failed = createRoutedProvider([
      provider("blaxel", { getById: async () => { throw error; } }),
      provider("arker", { getById: async () => { assert.fail("cached lookup failure must not fall through"); } }),
    ], path).instances!;
    await assert.rejects(failed.getById("shared"), (caught) => caught === error);
    assert.equal(await cache.get("shared"), "blaxel");
    const missing = createRoutedProvider([provider("blaxel"), provider("arker")], path).instances!;
    assert.equal(await missing.getById("shared"), null);
    assert.equal(await cache.get("shared"), null);
  }
}
await testCachedMissInvalidatesButErrorPreservesHint();
console.log("PASS compat router");


async function testKeyedCreationKeepsItsOwner(): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "arker-router-keyed-"));
  for (const weights of [{ arker: 50, e2b: 50 }, { e2b: 50, arker: 50 }]) {
    const path = await config(directory, {
      version: 1,
      existing: { strategy: "priority", order: ["e2b", "arker"] },
      create: { strategy: "weighted", weights, fallback: ["e2b", "arker"] },
    });
    for (let attempt = 0; attempt < 20; attempt++) {
      const routed = createRoutedProvider([provider("arker"), provider("e2b")], path, undefined, () => "repeat-key");
      assert.equal((await routed.instances!.create()).provider, "arker");
    }
    let calls = 0;
    const routed = createRoutedProvider([
      provider("arker", { create: async () => { calls++; throw new CompatibilityCreationNotStartedError("unavailable"); } }),
      provider("e2b", { create: async () => assert.fail("keyed create cannot change owner") }),
    ], path, undefined, () => "repeat-key");
    await assert.rejects(routed.instances!.create(), CompatibilityCreationNotStartedError);
    assert.equal(calls, 1);
  }
}
await testKeyedCreationKeepsItsOwner();
