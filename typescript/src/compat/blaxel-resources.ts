import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import {
  DriveInstance as NativeDrive, VolumeInstance as NativeVolume, createPaginatedList,
  type DriveListQuery, type VolumeListQuery,
} from "@blaxel/core";
import { Arker, ArkerError, type ArkerOptions, type Filesystem } from "../index.js";
import { createRoutedProvider, parseRouterConfig } from "./router.js";

type Resource = NativeDrive | NativeVolume;
type Create = Parameters<typeof NativeDrive.create>[0] & Parameters<typeof NativeVolume.create>[0];
type Query = DriveListQuery & VolumeListQuery;
export interface BlaxelResourceConfig { arker?: ArkerOptions; routerConfigPath?: string; }

function unsupported(message: string): never {
  throw new ArkerError("unsupported_operation", message, 422);
}
function missing(error: unknown): boolean {
  return error instanceof ArkerError ? error.status === 404 && error.code === "not_found"
    : !!error && typeof error === "object" && "code" in error && error.code === 404;
}
function only(value: Record<string, unknown>, allowed: string[]): void {
  for (const [key, item] of Object.entries(value)) {
    if (item !== undefined && !allowed.includes(key)) unsupported(`Unsupported Arker resource option: ${key}`);
  }
}
function driveName(config: Create): string {
  if (!config || typeof config !== "object" || Array.isArray(config)) throw new TypeError("Drive configuration must be an object");
  let name: unknown;
  if ("metadata" in config || "spec" in config) {
    only(config as unknown as Record<string, unknown>, ["metadata", "spec"]);
    const raw = config as unknown as { metadata?: Record<string, unknown>; spec?: Record<string, unknown>; };
    const metadata = raw.metadata;
    if (!metadata || typeof metadata !== "object" || !raw.spec || typeof raw.spec !== "object" || Array.isArray(metadata) || Array.isArray(raw.spec)) throw new TypeError("Drive metadata and spec must be objects");
    only(metadata, ["name"]);
    only(raw.spec, []);
    name = metadata.name;
  } else {
    only(config as Record<string, unknown>, ["name"]);
    name = config.name;
  }
  if (name === undefined || name === null || name === "") name = `drive-${randomUUID().replaceAll("-", "").slice(0, 8)}`;
  if (typeof name !== "string" || !/^[a-z0-9-]{1,49}$/.test(name)) throw new TypeError("Drive name must be 1–49 lowercase alphanumeric or hyphen characters");
  return name;
}

class Resources {
  readonly client?: Arker;
  readonly native: typeof NativeDrive | typeof NativeVolume;
  readonly router: ReturnType<typeof createRoutedProvider>;
  readonly listOrder: () => Promise<string[]>;
  constructor(readonly kind: "drive" | "volume", config: BlaxelResourceConfig) {
    this.native = kind === "drive" ? NativeDrive : NativeVolume;
    if (config.arker?.apiKey || process.env.ARKER_API_KEY?.trim()) this.client = new Arker(config.arker);
    const path = config.routerConfigPath ?? process.env.COMPUTESDK_ROUTER_CONFIG;
    const names = this.client ? ["arker", "blaxel"] : ["blaxel"];
    this.listOrder = () => path ? readFile(path, "utf8").then(text => parseRouterConfig(JSON.parse(text), names).existing.order) : Promise.resolve(names);
    this.router = createRoutedProvider([
      ...(this.client ? [{
        name: "arker", instances: {
          create: (request: { config: Create; reuse: boolean; }) => this.createArker(request.config, request.reuse),
          getById: (name: string) => this.lookupArker(name),
          destroy: async () => unsupported("Deletion requires an authoritative resource handle"),
        }
      }] : []),
      {
        name: "blaxel", instances: {
          create: (request: { config: Create; reuse: boolean; }) => request.reuse ? this.native.createIfNotExists(request.config) : this.native.create(request.config),
          getById: async (name: string) => {
            try { return await this.native.get(name); } catch (error) { if (missing(error)) return null; throw error; }
          },
          destroy: async (name: string) => { await this.native.delete(name); },
        }
      },
    ], path,
      { namespace: `blaxel-${kind}-name`, identifier: item => item?.name, requireUnique: true });
  }
  async files(name?: string): Promise<Filesystem[]> {
    const files: Filesystem[] = [];
    const seen = new Set<string>();
    let cursor: string | undefined = "0";
    do {
      if (seen.has(cursor)) throw new Error("Filesystem listing repeated a cursor");
      seen.add(cursor);
      const page = await this.client!.listFilesystems({ cursor, limit: 100, namePrefix: name });
      files.push(...page.filesystems);
      cursor = page.next_cursor ?? undefined;
    } while (cursor);
    return files;
  }
  bind(file: Filesystem): Resource {
    const model = { metadata: { name: file.name, createdAt: file.created_at }, spec: { ...(file.region ? { region: file.region } : {}) } };
    const instance = this.kind === "drive" ? new NativeDrive(model) : new NativeVolume(model);
    Object.defineProperties(instance, {
      delete: {
        value: async () => {
          const result = await this.client!._request<{ deleted: boolean; }>("DELETE", `/v1/filesystems/${encodeURIComponent(file.filesystem_id)}?only_if_unmounted=true`, undefined, this.client!.baseUrl, undefined, undefined, false);
          if (!result.deleted) throw { code: 404, error: `${this.kind} not found` };
          return this.kind === "drive" ? { name: file.name } : model;
        }
      },
      update: { value: async () => unsupported("Unsupported Arker resource update") },
    });
    return instance;
  }
  async lookupArker(name: string): Promise<Resource | null> {
    const matches = (await this.files(name)).filter(file => file.name === name);
    if (matches.length > 1) throw new Error("Filesystem name is ambiguous");
    if (!matches.length) return null;
    try {
      const current = await this.client!.getFilesystem(matches[0]!.filesystem_id);
      if (current.name !== name) throw new Error("Storage identity changed during lookup");
      return this.bind(current);
    }
    catch (error) { if (missing(error)) return null; throw error; }
  }
  async createArker(config: Create, reuse: boolean): Promise<Resource> {
    if (this.kind === "volume") unsupported("Arker filesystems cannot enforce the native volume capacity (including its default size)");
    const name = driveName(config);
    const requestedName = "name" in config ? config.name : "metadata" in config ? config.metadata.name : undefined;
    try {
      return this.bind(await this.client!._request<Filesystem>("POST", "/v1/filesystems", { name }, this.client!.baseUrl, undefined, undefined, false));
    } catch (error) {
      if (reuse && requestedName && error instanceof ArkerError && error.status === 409 && error.code === "already_exists") {
        const found = await this.lookupArker(name);
        if (found) return found;
      }
      throw error;
    }
  }
  create(config: Create, reuse = false): Promise<Resource> {
    return this.router.instances!.create({ config, reuse });
  }
  async get(name: string): Promise<Resource> {
    if (!await this.router.hasConfiguredProvider("arker")) return this.native.get(name);
    const found = await this.router.getOwner(name);
    if (!found) throw { code: 404, error: `${this.kind} not found` };
    return found.instance;
  }
  async delete(name: string) {
    if (!await this.router.hasConfiguredProvider("arker")) return this.native.delete(name);
    return (await this.get(name)).delete();
  }
  async update(name: string, updates: Create): Promise<Resource> {
    if (!await this.router.hasConfiguredProvider("arker")) return this.native.update(name, updates);
    const found = await this.router.getOwner(name);
    if (!found) throw { code: 404, error: `${this.kind} not found` };
    if (found.provider === "arker") unsupported("Unsupported Arker resource update");
    return this.native.update(name, updates);
  }
  async list(query?: Query) {
    const order = await this.listOrder();
    if (order.length === 1 && order[0] === "blaxel") return this.native.list(query);
    only(query ?? {}, ["limit", "cursor"]);
    const limit = query?.limit ?? 50;
    if (!Number.isInteger(limit) || limit <= 0) throw new TypeError("List limit must be a positive integer");
    const fetchPage = async (q?: Query) => {
      let index = 0, cursor: string | undefined = q?.cursor;
      if (order.length > 1 && cursor) {
        try {
          if (!/^[A-Za-z0-9_=-]+$/.test(cursor)) throw new Error();
          const decoded: unknown = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8"));
          if (!Array.isArray(decoded) || decoded.length !== 4) throw new Error();
          const [kind, previous, position, backend] = decoded;
          if (kind !== this.kind || JSON.stringify(previous) !== JSON.stringify(order)
            || !Number.isInteger(position) || position < 0 || position >= order.length
            || (backend !== null && typeof backend !== "string")) throw new Error();
          index = position; cursor = backend ?? undefined;
        } catch { throw new TypeError("Cursor does not belong to this storage listing"); }
      }
      const seen = new Set<string>();
      for (;;) {
        const key = JSON.stringify([index, cursor]);
        if (seen.has(key)) throw new Error("Storage listing repeated a cursor");
        seen.add(key);
        let data: Resource[], next: string | undefined;
        if (order[index] === "arker") {
          const page = await this.client!.listFilesystems({ limit, cursor: cursor ?? "0" });
          data = page.filesystems.map(file => this.bind(file));
          next = page.next_cursor ?? undefined;
        } else {
          const page = await this.native.list({ limit, ...(cursor ? { cursor } : {}) });
          data = page.data; next = page.nextCursor;
        }
        if (!next) index++;
        cursor = next;
        const more = index < order.length;
        if (!data.length && more) continue;
        const nextCursor = !more ? undefined : order.length === 1 ? cursor
          : Buffer.from(JSON.stringify([this.kind, order, index, cursor ?? null])).toString("base64")
            .replaceAll("+", "-").replaceAll("/", "_");
        return { data, meta: { hasMore: more, ...(nextCursor ? { nextCursor } : {}) } };
      }
    };
    return createPaginatedList({ response: await fetchPage(query), fetchPage, mapItem: item => item, query });
  }
}

export class DriveInstance extends NativeDrive {
  static override create(config: Parameters<typeof NativeDrive.create>[0], options: BlaxelResourceConfig = {}): Promise<NativeDrive> { return new Resources("drive", options).create(config) as Promise<NativeDrive>; }
  static override createIfNotExists(config: Parameters<typeof NativeDrive.create>[0], options: BlaxelResourceConfig = {}): Promise<NativeDrive> { return new Resources("drive", options).create(config, true) as Promise<NativeDrive>; }
  static override get(name: string, options: BlaxelResourceConfig = {}): Promise<NativeDrive> { return new Resources("drive", options).get(name) as Promise<NativeDrive>; }
  static override delete(name: string, options: BlaxelResourceConfig = {}): ReturnType<typeof NativeDrive.delete> { return new Resources("drive", options).delete(name) as ReturnType<typeof NativeDrive.delete>; }
  static override update(name: string, updates: Parameters<typeof NativeDrive.update>[1], options: BlaxelResourceConfig = {}): Promise<NativeDrive> { return new Resources("drive", options).update(name, updates) as Promise<NativeDrive>; }
  static override list(query?: DriveListQuery, options: BlaxelResourceConfig = {}): ReturnType<typeof NativeDrive.list> { return new Resources("drive", options).list(query) as ReturnType<typeof NativeDrive.list>; }
}
export class VolumeInstance extends NativeVolume {
  static override create(config: Parameters<typeof NativeVolume.create>[0], options: BlaxelResourceConfig = {}): Promise<NativeVolume> { return new Resources("volume", options).create(config) as Promise<NativeVolume>; }
  static override createIfNotExists(config: Parameters<typeof NativeVolume.create>[0], options: BlaxelResourceConfig = {}): Promise<NativeVolume> { return new Resources("volume", options).create(config, true) as Promise<NativeVolume>; }
  static override get(name: string, options: BlaxelResourceConfig = {}): Promise<NativeVolume> { return new Resources("volume", options).get(name) as Promise<NativeVolume>; }
  static override delete(name: string, options: BlaxelResourceConfig = {}): ReturnType<typeof NativeVolume.delete> { return new Resources("volume", options).delete(name) as ReturnType<typeof NativeVolume.delete>; }
  static override update(name: string, updates: Parameters<typeof NativeVolume.update>[1], options: BlaxelResourceConfig = {}): Promise<NativeVolume> { return new Resources("volume", options).update(name, updates) as Promise<NativeVolume>; }
  static override list(query?: VolumeListQuery, options: BlaxelResourceConfig = {}): ReturnType<typeof NativeVolume.list> { return new Resources("volume", options).list(query) as ReturnType<typeof NativeVolume.list>; }
}
