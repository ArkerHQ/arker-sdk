import { randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename, stat, unlink, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

type Provider = {
  name?: string;
  instances?: ProviderInstances;
  sandbox?: ProviderInstances;
};

type ProviderInstances = {
  create(options?: unknown): Promise<any>;
  getById(id: string): Promise<any | null>;
  destroy(id: string): Promise<void>;
};

export type RouterConfig = {
  version: 1;
  existing: { strategy: "race" | "priority"; order: string[] };
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
  rejectUnknown(existingValue, ["strategy", "order"], "router existing");
  if (existingValue.strategy !== "race" && existingValue.strategy !== "priority") {
    throw new Error("router existing.strategy must be race or priority");
  }
  const existing: RouterConfig["existing"] = {
    strategy: existingValue.strategy,
    order: names(existingValue.order, "router existing.order"),
  };

  const createValue = record(root.create, "router create");
  let create: RouterConfig["create"];
  if (createValue.strategy === "priority") {
    rejectUnknown(createValue, ["strategy", "order"], "router create");
    create = { strategy: "priority", order: names(createValue.order, "router create.order") };
  } else if (createValue.strategy === "weighted") {
    rejectUnknown(createValue, ["strategy", "weights", "fallback"], "router create");
    const values = record(createValue.weights, "router create.weights");
    const weights: Record<string, number> = {};
    for (const [provider, weight] of Object.entries(values)) {
      if (typeof weight !== "number" || !Number.isFinite(weight) || weight <= 0) {
        throw new Error(`router weight for ${provider} must be a positive finite number`);
      }
      weights[provider] = weight;
    }
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

function selectWeighted(weights: Record<string, number>): string {
  const scale = Math.max(...Object.values(weights));
  const entries = Object.entries(weights).map(([provider, weight]) => [provider, weight / scale] as const);
  let point = Math.min(Math.max(Math.random(), 0), 1 - Number.EPSILON)
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
      if (payload.version !== 1 || !payload.entries || typeof payload.entries !== "object") throw new Error();
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
      const entry = payload.entries[id];
      if (!entry) return null;
      entry.accessedAt = this.nextAccess(payload);
      return entry.provider;
    });
  }
  async set(id: string, provider: string) {
    await this.locked((payload) => { payload.entries[id] = { provider, accessedAt: this.nextAccess(payload) }; });
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
  if (!error || typeof error !== "object") return false;
  let current: any = error;
  const seen = new Set<object>();
  while (current !== undefined && current !== null) {
    if (typeof current !== "object" || seen.has(current)) return false;
    seen.add(current);
    const status = current.status ?? current.statusCode ?? current.response?.status;
    if (["ECONNRESET", "ECONNREFUSED", "EPIPE", "ENETUNREACH", "ETIMEDOUT", "UND_ERR_SOCKET"]
      .includes(String(current.code ?? "").toUpperCase())) return false;
    if (current.name === "AbortError" || [400, 401, 403, 422].includes(status)) return false;
    if (["authentication", "authorization", "unauthorized", "forbidden", "invalid_request", "validation"]
      .includes(String(current.code ?? "").toLowerCase())) return false;
    if (/(authentication failed|unauthorized|forbidden|invalid api key|missing .*api key)/i.test(current.message ?? "")) return false;
    if (/(outcome is unknown|may have (?:succeeded|been created)|network failure|transport error|timed? out|timeout|fetch failed|connection (?:reset|refused|closed)|socket hang up|broken pipe|all connection attempts failed)/i
      .test(current.message ?? "")) return false;
    current = current.cause;
  }
  return true;
}

export function createRoutedProvider(providers: Provider[], routerConfigPath?: string): Provider {
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
    try { id = instanceId(instance); } catch { return instance; }
    if (id) {
      await cache.set(id, provider.name!).catch(() => undefined);
    }
    return instance;
  };
  const find = async (id: string): Promise<{ instance: any; provider: Provider } | null> => {
    const router = await configured;
    let cachedName = await cache.get(id).catch(() => null);
    const eligible = (name: string) => providerAcceptsIdentifier(name, id, router.existing.order.includes("arker"));
    if (cachedName && !eligible(cachedName)) {
      await cache.delete(id).catch(() => undefined);
      cachedName = null;
    }
    if (cachedName && byName.has(cachedName) && router.existing.order.includes(cachedName)) {
      const provider = byName.get(cachedName)!;
      const instance = await manager(provider).getById(id);
      if (instance) return { instance: await remember(instance, provider), provider };
      await cache.delete(id).catch(() => undefined);
    }
    const candidates = router.existing.order.filter((name) => name !== cachedName && eligible(name)).map((name) => byName.get(name)!);
    if (router.existing.strategy === "race") {
      return new Promise((resolveRace, rejectRace) => {
        let remaining = candidates.length;
        let finished = false;
        const errors: unknown[] = [];
        if (remaining === 0) resolveRace(null);
        for (const provider of candidates) {
          manager(provider).getById(id).then(async (instance) => {
            if (finished) return;
            if (instance) {
              finished = true;
              await remember(instance, provider);
              resolveRace({ instance, provider });
              return;
            }
            remaining -= 1;
            if (remaining === 0) {
              finished = true;
              if (errors.length) rejectRace(Object.assign(new Error(`Lookup failed for ${id}`), { errors }));
              else resolveRace(null);
            }
          }).catch((error) => {
            if (finished) return;
            errors.push(error);
            remaining -= 1;
            if (remaining === 0) {
              finished = true;
              rejectRace(Object.assign(new Error(`Lookup failed for ${id}`), { errors }));
            }
          });
        }
      });
    }
    for (const provider of candidates) {
      const instance = await manager(provider).getById(id);
      if (instance) return { instance: await remember(instance, provider), provider };
    }
    return null;
  };

  return {
    name: "arker-compat-router",
    instances: {
      async create(options?: unknown) {
        const router = await configured;
        const order = router.create.strategy === "priority"
          ? router.create.order
          : [
            selectWeighted(router.create.weights),
            ...(router.create.fallback ?? Object.keys(router.create.weights)),
          ].filter((name, index, values) => values.indexOf(name) === index);
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
      async getById(id: string) { return (await find(id))?.instance ?? null; },
      async destroy(id: string) {
        const known = (await find(id))?.provider;
        if (!known) return;
        await manager(known).destroy(id);
        await cache.delete(id).catch(() => undefined);
      },
    },
  };
}
