import {
  DaytonaBadRequestError,
  DaytonaAuthenticationError,
  DaytonaBadGatewayError,
  DaytonaConflictError,
  DaytonaError,
  DaytonaForbiddenError,
  DaytonaGoneError,
  DaytonaInternalServerError,
  DaytonaNotFoundError,
  DaytonaRateLimitError,
  DaytonaServiceUnavailableError,
  DaytonaTimeoutError,
  DaytonaUnprocessableEntityError,
  type Daytona,
  type VolumeMount,
} from "@daytona/sdk";
import { posix } from "node:path";

import { Arker, ArkerError, type ArkerOptions, type Filesystem, type VM } from "../index.js";
import { shellQuote, type UniversalSandbox } from "./common.js";
import { daytonaGuestCommand } from "./daytona-filesystem.js";
import mountReadyScript from "./e2b-mount-ready.generated.js";
import { createRoutedProvider } from "./router.js";

type NativeVolumeService = Daytona["volume"];
type Volume = Awaited<ReturnType<NativeVolumeService["list"]>>[number];
type VolumeOwner = { service: DaytonaVolumeService; provider: "arker" | "daytona" };

const owners = new WeakMap<object, VolumeOwner>();

function missing(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const value = error as { status?: number; statusCode?: number; response?: { status?: number } };
  return (value.statusCode ?? value.status ?? value.response?.status) === 404;
}

function unsupported(message: string): never {
  throw new ArkerError("unsupported_operation", message, 422);
}

export function translateArker(error: unknown, conflictIsBadRequest = false): never {
  if (!(error instanceof ArkerError)) throw error;
  const status = conflictIsBadRequest && error.status === 409 && error.code === "already_exists" ? 400 : error.status;
  if (status === 400) throw new DaytonaBadRequestError(error.message, status);
  if (status === 401) throw new DaytonaAuthenticationError(error.message, status);
  if (status === 403) throw new DaytonaForbiddenError(error.message, status);
  if (status === 404) throw new DaytonaNotFoundError(error.message, status);
  if (status === 408 || status === 504) throw new DaytonaTimeoutError(error.message, status);
  if (status === 409) throw new DaytonaConflictError(error.message, status);
  if (status === 410) throw new DaytonaGoneError(error.message, status);
  if (status === 422) throw new DaytonaUnprocessableEntityError(error.message, status);
  if (status === 429) throw new DaytonaRateLimitError(error.message, status);
  if (status === 500) throw new DaytonaInternalServerError(error.message, status);
  if (status === 502) throw new DaytonaBadGatewayError(error.message, status);
  if (status === 503) throw new DaytonaServiceUnavailableError(error.message, status);
  throw new DaytonaError(error.message, status, undefined, error.code);
}

function validateMountPath(path: unknown): asserts path is string {
  if (typeof path !== "string" || Buffer.byteLength(path) > 256 || path === "/" || !path.startsWith("/") || path.includes("\0") || posix.normalize(path) !== path) {
    throw new TypeError("Volume mount path must be canonical, absolute and non-root");
  }
}

function normalizeMountSubpath(value: unknown): string | undefined {
  if (value === undefined || value === "") return undefined;
  if (
    typeof value !== "string"
    || Buffer.byteLength(value) > 512
    || value.startsWith("/")
    || value.includes("\\")
    || /\p{Cc}/u.test(value)
    || value.includes("*")
    || value.includes("?")
    || value.includes("${")
    || value.split("/").some((part) => part === "" || part === "." || part === "..")
  ) {
    throw new TypeError("Volume subpath must be a canonical relative path");
  }
  return value;
}

export function normalizeDaytonaVolumeMounts(value: unknown): VolumeMount[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) throw new TypeError("volumes must be an array");
  const paths = new Set<string>();
  return value.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw new TypeError("Invalid Daytona volume mount");
    const unknown = Object.keys(item).find((key) => !["volumeId", "mountPath", "subpath"].includes(key));
    if (unknown) throw new TypeError(`Invalid Daytona volume mount field ${unknown}`);
    const mount = item as VolumeMount & { subpath?: unknown };
    validateMountPath(mount.mountPath);
    if (paths.has(mount.mountPath)) throw new TypeError("Duplicate volume mount path");
    paths.add(mount.mountPath);
    if (typeof mount.volumeId !== "string" || !mount.volumeId) throw new TypeError("volumeId must be a nonempty string");
    const subpath = normalizeMountSubpath(mount.subpath);
    return { volumeId: mount.volumeId, mountPath: mount.mountPath, ...(subpath !== undefined ? { subpath } : {}) };
  });
}

function readyCommand(paths: string[]): string {
  return "python3 -I -c " + shellQuote(mountReadyScript) + " " + ["60", ...paths].map(shellQuote).join(" ");
}

function arkerVolume(info: Filesystem, service: DaytonaVolumeService): Volume {
  const volume = {
    id: info.filesystem_id,
    name: info.name,
    organizationId: info.owner_org_id,
    state: "ready",
    createdAt: info.created_at,
    updatedAt: info.created_at,
    lastUsedAt: null,
    errorReason: null,
  } as unknown as Volume;
  owners.set(volume, { service, provider: "arker" });
  return volume;
}

export class DaytonaVolumeService {
  readonly #arker?: Arker;
  readonly #native: () => NativeVolumeService;
  readonly #router: ReturnType<typeof createRoutedProvider>;
  readonly #mountRouter: ReturnType<typeof createRoutedProvider>;

  constructor(
    arker: ArkerOptions | undefined,
    native: () => NativeVolumeService,
    routerConfigPath?: string,
    nativeEnabled = true,
  ) {
    this.#native = native;
    const hasArker = !!(arker?.apiKey || globalThis.process?.env?.ARKER_API_KEY?.trim());
    if (hasArker) this.#arker = new Arker(arker);
    const arkerProvider = {
      name: "arker",
      instances: {
        create: (name: unknown) => this.#createArker(name),
        getById: (name: string) => this.#getArkerByName(name, false),
        destroy: async () => unsupported("Arker filesystem deletion cannot atomically refuse mounted volumes; use the explicit filesystem API"),
        list: () => this.#listArker(),
      },
    };
    const nativeProvider = {
      name: "daytona",
      instances: {
        create: async (name: unknown) => this.#bindNative(await this.#native().create(name as string)),
        getById: async (name: string) => {
          try { return this.#bindNative(await this.#native().get(name)); }
          catch (error) { if (missing(error)) return null; throw error; }
        },
        destroy: async () => unsupported("Volume deletion requires its original provider-bound handle"),
        list: async () => Promise.all((await this.#native().list()).map((volume) => this.#bindNative(volume))),
      },
    };
    this.#router = createRoutedProvider(
      [...(hasArker ? [arkerProvider] : []), ...(nativeEnabled ? [nativeProvider] : [])],
      routerConfigPath ?? globalThis.process?.env?.COMPUTESDK_ROUTER_CONFIG,
      { namespace: "daytona-volume-name", identifier: (volume) => volume?.id, requireUnique: true },
    );
    const mountArkerProvider = {
      ...arkerProvider,
      instances: { ...arkerProvider.instances, getById: (identifier: string) => this.#getArkerByName(identifier, true) },
    };
    const mountNativeProvider = {
      ...nativeProvider,
      instances: {
        ...nativeProvider.instances,
        getById: async (identifier: string) => {
          const matches = (await this.#native().list()).filter((volume) => volume.id === identifier || volume.name === identifier);
          if (matches.length > 1) throw new Error("Volume mount identifier matches multiple native volumes");
          return matches[0] ? this.#bindNative(matches[0]) : null;
        },
      },
    };
    this.#mountRouter = createRoutedProvider(
      [...(hasArker ? [mountArkerProvider] : []), ...(nativeEnabled ? [mountNativeProvider] : [])],
      routerConfigPath ?? globalThis.process?.env?.COMPUTESDK_ROUTER_CONFIG,
      { namespace: "daytona-volume-mount", identifier: (volume) => volume?.id, requireUnique: true },
    );
  }

  #bindNative(volume: Volume): Volume {
    owners.set(volume, { service: this, provider: "daytona" });
    return volume;
  }

  async #createArker(name: unknown): Promise<Volume> {
    try { return arkerVolume(await this.#arker!.createFilesystem({ name: name as string }), this); }
    catch (error) { translateArker(error, true); }
  }

  async #listArker(): Promise<Volume[]> {
    const result: Volume[] = [];
    const seen = new Set<string>();
    let cursor: string | undefined = "0";
    do {
      let page;
      try { page = await this.#arker!.listFilesystems({ cursor, limit: 100 }); }
      catch (error) { translateArker(error); }
      result.push(...page.filesystems.map((info) => arkerVolume(info, this)));
      cursor = page.next_cursor ?? undefined;
      if (!cursor && page.filesystems.length >= 500) unsupported("Filesystem listing may be truncated at the server limit");
      if (cursor && seen.has(cursor)) throw new Error("Filesystem listing repeated a cursor");
      if (cursor) seen.add(cursor);
    } while (cursor);
    return result;
  }

  async #getArkerByName(name: string, allowId: boolean): Promise<Volume | null> {
    const matches = (await this.#listArker()).filter((volume) => volume.name === name || (allowId && volume.id === name));
    if (matches.length === 0) return null;
    if (matches.length !== 1) throw new Error("Volume name is ambiguous");
    try { return arkerVolume(await this.#arker!.getFilesystem(matches[0]!.id), this); }
    catch (error) { translateArker(error); }
  }

  async create(name: string): Promise<Volume> {
    return this.#router.instances!.create(name);
  }

  async get(name: string, create = false): Promise<Volume> {
    const volume = await this.#router.instances!.getById(name);
    if (volume) return volume;
    if (create) return this.create(name);
    throw new DaytonaNotFoundError("Volume not found", 404);
  }

  async list(): Promise<Volume[]> {
    return this.#router.list();
  }

  async delete(volume: Volume): Promise<void> {
    const owner = volume && typeof volume === "object" ? owners.get(volume) : undefined;
    if (owner?.provider === "arker") {
      try { await owner.service.#arker!.deleteFilesystem(volume.id, { onlyIfUnmounted: true }); }
      catch (error) { translateArker(error); }
      return;
    }
    if (!owner && typeof volume?.id === "string" && volume.id.startsWith("dir_")) {
      unsupported("Arker filesystem deletion requires its original provider-bound handle");
    }
    if (owner && owner.service !== this) {
      await owner.service.delete(volume);
      return;
    }
    await this.#native().delete(volume);
  }

  async resolveMounts(value: unknown): Promise<Array<{ filesystemId: string; mountPath: string; subpath?: string }>> {
    const mounts = normalizeDaytonaVolumeMounts(value);
    if (mounts === undefined) return [];
    const resolved: Array<{ filesystemId: string; mountPath: string; subpath?: string }> = [];
    for (const mount of mounts) {
      if (mount.volumeId.startsWith("dir_")) {
        if (!this.#arker) unsupported("Arker filesystem IDs require an Arker sandbox");
        try {
          const info = await this.#arker.getFilesystem(mount.volumeId);
          resolved.push({ filesystemId: info.filesystem_id, mountPath: mount.mountPath, ...(mount.subpath ? { subpath: mount.subpath } : {}) });
        } catch (error) { translateArker(error); }
        continue;
      }
      const found = await this.#mountRouter.getOwner(mount.volumeId);
      if (!found) throw new DaytonaNotFoundError("Volume not found", 404);
      const volume = found.instance as Volume;
      const owner = owners.get(volume);
      if (owner?.service !== this || owner.provider !== "arker") unsupported("Native Daytona volumes require a native sandbox");
      resolved.push({ filesystemId: volume.id, mountPath: mount.mountPath, ...(mount.subpath ? { subpath: mount.subpath } : {}) });
    }
    return resolved;
  }

  async assertNativeMounts(value: unknown): Promise<void> {
    // Native Daytona owns payload validation; only enforce provider ownership here.
    if (!Array.isArray(value)) return;
    for (const mount of value) {
      const identifier = mount?.volumeId;
      if (typeof identifier !== "string" || !identifier) continue;
      if (identifier.startsWith("dir_")) unsupported("Arker filesystem IDs require an Arker sandbox");
      if (!await this.#mountRouter.hasConfiguredProvider("arker")) continue;
      const found = await this.#mountRouter.getOwner(identifier);
      if (found?.provider === "arker") unsupported("Arker volumes require an Arker sandbox");
    }
  }

  async attach(box: UniversalSandbox, mounts: Array<{ filesystemId: string; mountPath: string; subpath?: string }>): Promise<void> {
    if (mounts.length === 0) return;
    const vm = (box as UniversalSandbox & { getInstance(): unknown }).getInstance() as VM;
    try {
      for (const mount of mounts) await vm.createMount(mount);
      const result = await box.runCommand(daytonaGuestCommand({ operation: "exec", command: readyCommand(mounts.map((mount) => mount.mountPath)) }));
      if (result.exitCode !== 0) throw new Error("Daytona volume mounts did not become accessible to the guest user");
    } catch (error) {
      try { await box.destroy(); }
      catch (cleanup) { throw new AggregateError([error, cleanup], `Sandbox creation and cleanup failed for VM ${box.sandboxId}`); }
      throw error;
    }
  }
}
