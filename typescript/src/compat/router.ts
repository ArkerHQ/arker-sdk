import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename, stat, unlink, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { CompatibilityCreationNotStartedError } from "./common.js";
import { RouterLookupTimeoutError } from "./router-errors.js";
export { RouterLookupTimeoutError } from "./router-errors.js";

type Provider = {
  name?: string;
  ownershipProbe?: (id: string) => Promise<boolean>;
  instances?: ProviderInstances;
  sandbox?: ProviderInstances;
};

type ProviderInstances = {
  create(options?: unknown): Promise<any>;
  getById(id: string): Promise<any | null>;
  destroy(id: string): Promise<void>;
  list?(): Promise<any[]>;
};

export type RouterConfig = {
  version: 1;
  existing: { strategy: "race" | "priority"; order: string[]; timeoutMs?: number };
  create:
    | { strategy: "priority"; order: string[] }
    | { strategy: "weighted"; weights: Record<string, number>; fallback?: string[] };
  cache?: { path?: string; maxBytes?: number; maxEntries?: number };
};

type CachePayload = {
  version: 1;
  entries: Record<string, { provider: string; accessedAt: number }>;
};

interface RouteCache {
  get(id: string): Promise<string | null>;
  set(id: string, provider: string): Promise<void>;
  delete(id: string): Promise<void>;
}

const DEFAULT_CACHE_BYTES = 4 * 1024 * 1024;
const DEFAULT_CACHE_ENTRIES = 20_000;

export function isArkerVmIdentifier(id: string): boolean {
  // Identifier syntax is not ownership; the Arker adapter still authorizes lookup.
  return (id.length === 26 || id.length === 47)
    && /^(?:vmh-[0-9a-f]{16}-)?[0-7][0-9a-hjkmnp-tv-z]{25}$/i.test(id);
}

export function providerAcceptsIdentifier(provider: string, id: string, arkerEnabled: boolean): boolean {
  // E2B and Modal reject Arker's ID grammar rather than returning absence.
  return !(arkerEnabled && ["e2b", "modal"].includes(provider) && isArkerVmIdentifier(id));
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be an object`);
  return value as Record<string, unknown>;
}

function names(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.length === 0 || value.some((name) => typeof name !== "string" || !name)) {
    throw new Error(`${label} must be a non-empty array of provider names`);
  }
  if (new Set(value).size !== value.length) throw new Error(`${label} contains duplicate providers`);
  return value;
}

function rejectUnknown(value: Record<string, unknown>, allowed: string[], label: string): void {
  const unknown = Object.keys(value).find((key) => !allowed.includes(key));
  if (unknown) throw new Error(`${label} has unknown field ${unknown}`);
}

export function parseRouterConfig(value: unknown, providers: Iterable<string>): RouterConfig {
  const root = record(value, "router config");
  rejectUnknown(root, ["version", "existing", "create", "cache"], "router config");
  if (root.version !== 1) throw new Error(`unsupported router config version ${String(root.version)}`);

  const existingValue = record(root.existing, "router existing");
  rejectUnknown(existingValue, ["strategy", "order", "timeoutMs"], "router existing");
  if (existingValue.strategy !== "race" && existingValue.strategy !== "priority") {
    throw new Error("router existing.strategy must be race or priority");
  }
  const existing: RouterConfig["existing"] = {
    strategy: existingValue.strategy,
    order: names(existingValue.order, "router existing.order"),
  };
  if (existingValue.timeoutMs !== undefined) {
    if (!Number.isSafeInteger(existingValue.timeoutMs) || (existingValue.timeoutMs as number) <= 0) {
      throw new Error("router existing.timeoutMs must be a positive safe integer");
    }
    existing.timeoutMs = existingValue.timeoutMs as number;
  }

  const createValue = record(root.create, "router create");
  let create: RouterConfig["create"];
  if (createValue.strategy === "priority") {
    rejectUnknown(createValue, ["strategy", "order"], "router create");
    create = { strategy: "priority", order: names(createValue.order, "router create.order") };
  } else if (createValue.strategy === "weighted") {
    rejectUnknown(createValue, ["strategy", "weights", "fallback"], "router create");
    const values = record(createValue.weights, "router create.weights");
    const weights = Object.fromEntries(Object.entries(values).map(([provider, weight]) => {
      if (typeof weight !== "number" || !Number.isFinite(weight) || weight <= 0) {
        throw new Error(`router weight for ${provider} must be a positive finite number`);
      }
      return [provider, weight];
    }));
    if (Object.keys(weights).length === 0) throw new Error("router create.weights must not be empty");
    create = {
      strategy: "weighted",
      weights,
      fallback: createValue.fallback === undefined
        ? Object.keys(weights)
        : names(createValue.fallback, "router create.fallback"),
    };
  } else {
    throw new Error("router create.strategy must be priority or weighted");
  }

  let cache: RouterConfig["cache"];
  if (root.cache !== undefined) {
    const cacheValue = record(root.cache, "router cache");
    rejectUnknown(cacheValue, ["path", "maxBytes", "maxEntries"], "router cache");
    if (cacheValue.path !== undefined && (typeof cacheValue.path !== "string" || !cacheValue.path)) {
      throw new Error("router cache.path must be a non-empty string");
    }
    for (const field of ["maxBytes", "maxEntries"] as const) {
      const amount = cacheValue[field];
      if (amount !== undefined && (!Number.isInteger(amount) || (amount as number) <= 0)) {
        throw new Error(`router cache.${field} must be a positive integer`);
      }
    }
    cache = {
      path: cacheValue.path as string | undefined,
      maxBytes: cacheValue.maxBytes as number | undefined,
      maxEntries: cacheValue.maxEntries as number | undefined,
    };
  }

  const referenced = new Set([
    ...existing.order,
    ...(create.strategy === "priority" ? create.order : [...Object.keys(create.weights), ...(create.fallback ?? [])]),
  ]);
  const configured = new Set(providers);
  for (const provider of referenced) {
    if (!configured.has(provider)) throw new Error(`router config references unknown provider "${provider}"`);
  }
  return { version: 1, existing, create, ...(cache ? { cache } : {}) };
}

function selectWeighted(weights: Record<string, number>, sample = Math.random()): string {
  const scale = Math.max(...Object.values(weights));
  const entries = Object.entries(weights).map(([provider, weight]) => [provider, weight / scale] as const);
  let point = Math.min(Math.max(sample, 0), 1 - Number.EPSILON)
    * entries.reduce((total, [, weight]) => total + weight, 0);
  for (const [provider, weight] of entries) {
    if (point < weight) return provider;
    point -= weight;
  }
  return entries.at(-1)![0];
}

class MemoryRouteCache implements RouteCache {
  private readonly entries = new Map<string, string>();
  private bytes = 0;
  constructor(private readonly maxEntries = DEFAULT_CACHE_ENTRIES, private readonly maxBytes = DEFAULT_CACHE_BYTES) {}
  private size(id: string, provider: string): number { return Buffer.byteLength(JSON.stringify([id, provider])); }
  async get(id: string) {
    const provider = this.entries.get(id);
    if (provider === undefined) return null;
    this.entries.delete(id);
    this.entries.set(id, provider);
    return provider;
  }
  async set(id: string, provider: string) {
    this.remove(id);
    this.entries.set(id, provider);
    this.bytes += this.size(id, provider);
    while (this.entries.size > this.maxEntries || this.bytes > this.maxBytes) {
      this.remove(this.entries.keys().next().value!);
    }
  }
  private remove(id: string) {
    const provider = this.entries.get(id);
    if (provider !== undefined) this.bytes -= this.size(id, provider);
    this.entries.delete(id);
  }
  async delete(id: string) { this.remove(id); }
}

export class JsonRouteCache implements RouteCache {
  private readonly path: string;
  private readonly lockPath: string;
  constructor(
    path: string,
    private readonly maxEntries = DEFAULT_CACHE_ENTRIES,
    private readonly maxBytes = DEFAULT_CACHE_BYTES,
    private readonly now: () => number = Date.now,
  ) {
    this.path = resolve(path);
    this.lockPath = `${this.path}.lock`;
  }

  private async read(): Promise<CachePayload> {
    try {
      const payload = JSON.parse(await readFile(this.path, "utf8")) as CachePayload;
      if (payload.version !== 1 || !payload.entries || typeof payload.entries !== "object" || Array.isArray(payload.entries)) throw new Error();
      payload.entries = Object.fromEntries(Object.entries(payload.entries).filter(([id, entry]) => (
        !!id && !!entry && typeof entry.provider === "string"
        && typeof entry.accessedAt === "number" && Number.isFinite(entry.accessedAt)
      )));
      return payload;
    } catch {
      return { version: 1, entries: {} };
    }
  }

  private trim(payload: CachePayload): void {
    const oldest = () => Object.entries(payload.entries).sort((left, right) => left[1].accessedAt - right[1].accessedAt)[0];
    while (Object.keys(payload.entries).length > this.maxEntries) delete payload.entries[oldest()![0]];
    while (Object.keys(payload.entries).length > 0 && Buffer.byteLength(JSON.stringify(payload)) > this.maxBytes) {
      delete payload.entries[oldest()![0]];
    }
  }

  private async locked<T>(operation: (payload: CachePayload) => T): Promise<T> {
    await mkdir(dirname(this.path), { recursive: true });
    const token = randomUUID();
    let lock;
    for (let attempt = 0; ; attempt += 1) {
      try {
        lock = await open(this.lockPath, "wx", 0o600);
        await lock.writeFile(token);
        break;
      } catch (error: any) {
        if (error?.code !== "EEXIST") throw error;
        const age = await stat(this.lockPath).then((value) => Date.now() - value.mtimeMs).catch(() => 0);
        if (age > 30_000) {
          await unlink(this.lockPath).catch(() => undefined);
          continue;
        }
        if (attempt >= 100) throw new Error(`Timed out acquiring route cache lock ${this.lockPath}`);
        await new Promise((done) => setTimeout(done, Math.min(2 + attempt, 25)));
      }
    }
    try {
      const payload = await this.read();
      const result = operation(payload);
      this.trim(payload);
      if (Buffer.byteLength(JSON.stringify(payload)) > this.maxBytes) {
        await unlink(this.path).catch((error) => { if (error.code !== "ENOENT") throw error; });
        return result;
      }
      const temporary = `${this.path}.${process.pid}.${randomUUID()}.tmp`;
      await writeFile(temporary, JSON.stringify(payload), { mode: 0o600 });
      await rename(temporary, this.path);
      return result;
    } finally {
      await lock.close();
      if (await readFile(this.lockPath, "utf8").catch(() => "") === token) await unlink(this.lockPath).catch(() => undefined);
    }
  }

  async get(id: string) {
    return this.locked((payload) => {
      const entry = Object.hasOwn(payload.entries, id) ? payload.entries[id] : undefined;
      if (!entry) return null;
      entry.accessedAt = this.nextAccess(payload);
      return entry.provider;
    });
  }
  async set(id: string, provider: string) {
    await this.locked((payload) => { Object.defineProperty(payload.entries, id, {
      value: { provider, accessedAt: this.nextAccess(payload) }, enumerable: true, configurable: true, writable: true,
    }); });
  }
  private nextAccess(payload: CachePayload): number {
    let latest = this.now();
    for (const entry of Object.values(payload.entries)) latest = Math.max(latest, entry.accessedAt + 1);
    return latest;
  }
  async delete(id: string) {
    await this.locked((payload) => { delete payload.entries[id]; });
  }
}

function manager(provider: Provider): ProviderInstances {
  const value = provider.instances ?? provider.sandbox;
  if (!value) throw new Error(`provider ${provider.name ?? "unknown"} has no instance manager`);
  return value;
}

function instanceId(instance: any): string | undefined {
  return instance?.instanceId ?? instance?.sandboxId ?? instance?.id;
}

function retryable(error: unknown): boolean {
  // A remote failure can follow successful creation, even when it reports 429.
  return error instanceof CompatibilityCreationNotStartedError;
}

export function createRoutedProvider(
  providers: Provider[],
  routerConfigPath?: string,
  resource?: { namespace: string; identifier(instance: any): string | undefined; requireUnique?: boolean },
  creationKey?: (options: unknown) => string | undefined,
): Provider & {
  list(): Promise<any[]>;
  getOwner(id: string): Promise<{ provider: string; instance?: any } | null>;
  hasConfiguredProvider(name: string): Promise<boolean>;
} {
  const cacheKey = (id: string) => resource ? `${resource.namespace}:${id}` : id;
  const byName = new Map(providers.map((provider) => [provider.name!, provider]));
  if (byName.size !== providers.length || byName.has(undefined as any)) throw new Error("compatibility providers need unique names");
  let cache: RouteCache = new MemoryRouteCache();
  const ready = routerConfigPath
    ? readFile(routerConfigPath, "utf8").then((contents) => parseRouterConfig(JSON.parse(contents), byName.keys()))
    : Promise.resolve<RouterConfig>({
      version: 1,
      existing: { strategy: "priority", order: providers.map((provider) => provider.name!) },
      create: { strategy: "priority", order: providers.map((provider) => provider.name!) },
    });
  const configured = ready.then((router) => {
    cache = router.cache?.path
      ? new JsonRouteCache(router.cache.path, router.cache.maxEntries, router.cache.maxBytes)
      : new MemoryRouteCache(router.cache?.maxEntries, router.cache?.maxBytes);
    return router;
  });
  const remember = async (instance: any, provider: Provider) => {
    let id: string | undefined;
    try { id = resource ? resource.identifier(instance) : instanceId(instance); } catch { return instance; }
    if (id) {
      await cache.set(cacheKey(id), provider.name!).catch(() => undefined);
    }
    return instance;
  };
  type Located = { instance?: any; provider: Provider };
  const lookup = async (provider: Provider, id: string): Promise<Located | null> => {
    if (provider.ownershipProbe) return await provider.ownershipProbe(id) ? { provider } : null;
    const instance = await manager(provider).getById(id);
    return instance ? { instance, provider } : null;
  };
  const rememberOwner = async (id: string, located: Located): Promise<Located> => {
    await cache.set(cacheKey(id), located.provider.name!).catch(() => undefined);
    return located;
  };
  const find = async (id: string): Promise<Located | null> => {
    const router = await configured;
    const started = performance.now();
    const timeoutMs = router.existing.timeoutMs ?? 30_000;
    const probe = (provider: Provider): Promise<Located | null> => new Promise((resolveProbe, rejectProbe) => {
      let timer: ReturnType<typeof setTimeout>;
      const check = () => {
        const remaining = timeoutMs - (performance.now() - started);
        if (remaining <= 0) rejectProbe(new RouterLookupTimeoutError(id));
        else timer = setTimeout(check, Math.min(remaining, 2_147_483_647));
      };
      if (performance.now() - started >= timeoutMs) {
        rejectProbe(new RouterLookupTimeoutError(id));
        return;
      }
      check();
      lookup(provider, id).then((located) => {
        if (performance.now() - started >= timeoutMs) rejectProbe(new RouterLookupTimeoutError(id));
        else resolveProbe(located);
      }, (error) => {
        if (performance.now() - started >= timeoutMs) rejectProbe(new RouterLookupTimeoutError(id));
        else rejectProbe(error);
      }).finally(() => clearTimeout(timer));
    });
    let cachedName = resource?.requireUnique ? null : await cache.get(cacheKey(id)).catch(() => null);
    const eligible = (name: string) => !!resource || providerAcceptsIdentifier(name, id, router.existing.order.includes("arker"));
    if (cachedName && (!byName.has(cachedName) || !router.existing.order.includes(cachedName) || !eligible(cachedName))) {
      await cache.delete(cacheKey(id)).catch(() => undefined);
      cachedName = null;
    }
    if (cachedName && byName.has(cachedName) && router.existing.order.includes(cachedName)) {
      const provider = byName.get(cachedName)!;
      const located = await probe(provider);
      if (located) return rememberOwner(id, located);
      await cache.delete(cacheKey(id)).catch(() => undefined);
    }
    const candidates = router.existing.order.filter((name) => name !== cachedName && eligible(name)).map((name) => byName.get(name)!);
    if (resource?.requireUnique || router.existing.strategy === "race") {
      const completed = await Promise.allSettled(candidates.map(probe));
      for (const result of completed) {
        if (result.status === "rejected") throw result.reason;
      }
      const matches = completed.flatMap((result) => result.status === "fulfilled" && result.value ? [result.value] : []);
      if (resource?.requireUnique && matches.length > 1) {
        throw new Error(`Resource name ${JSON.stringify(id)} is ambiguous across configured providers`);
      }
      if (matches[0]) return rememberOwner(id, matches[0]);
      return null;
    }
    for (const provider of candidates) {
      const located = await probe(provider);
      if (located) return rememberOwner(id, located);
    }
    return null;
  };

  return {
    name: "arker-compat-router",
    async hasConfiguredProvider(name: string) {
      return (await configured).existing.order.includes(name);
    },
    async list() {
      const router = await configured;
      const results = [];
      for (const name of router.existing.order) {
        const service = manager(byName.get(name)!);
        if (!service.list) throw new Error(`Provider ${name} does not support resource listing`);
        results.push(...await service.list());
      }
      return results;
    },
    async getOwner(id: string) {
      const located = await find(id);
      return located ? { provider: located.provider.name!, instance: located.instance } : null;
    },
    instances: {
      async create(options?: unknown) {
        const router = await configured;
        const key = creationKey?.(options);
        const weighted = router.create.strategy === "weighted" ? router.create.weights : {};
        const first = () => key === undefined ? selectWeighted(weighted) : selectWeighted(
          Object.fromEntries(Object.entries(weighted).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)),
          parseInt(createHash("sha256").update(key).digest("hex").slice(0, 13), 16) / 2 ** 52,
        );
        let order = router.create.strategy === "priority"
          ? router.create.order
          : [
            first(),
            ...(router.create.fallback ?? Object.keys(router.create.weights)),
          ].filter((name, index, values) => values.indexOf(name) === index);
        if (key !== undefined) order = order.slice(0, 1);
        let lastError: unknown;
        for (const name of order) {
          const provider = byName.get(name)!;
          try {
            return await remember(await manager(provider).create(options), provider);
          } catch (error) {
            if (!retryable(error)) throw error;
            lastError = error;
          }
        }
        throw lastError ?? new Error("No compatibility provider could create a sandbox");
      },
      async getById(id: string) {
        const located = await find(id);
        if (!located) return null;
        if (located.instance) return located.instance;
        const instance = await manager(located.provider).getById(id);
        if (!instance) await cache.delete(cacheKey(id)).catch(() => undefined);
        return instance;
      },
      async destroy(id: string) {
        const known = (await find(id))?.provider;
        if (!known) return;
        await manager(known).destroy(id);
        await cache.delete(cacheKey(id)).catch(() => undefined);
      },
    },
  };
}
