/** Arker volume primitives after provider selection. Mount strings are names.
 * Attachment records do not certify guest readiness or cross-VM POSIX coherence.
 */
import { Arker, type ArkerOptions, type VM } from "../index.js";
import { Volume as NativeVolume, VolumeNotFoundError, type ConnectionOpts } from "e2b";
import { createRoutedProvider } from "./router.js";
import mountReadyScript from "./e2b-mount-ready.generated.js";
import { shellQuote } from "./common.js";

function validateName(name: string): void {
  if (typeof name !== "string" || name.length < 1 || name.length > 128 ||
      /[^A-Za-z0-9_.-]/.test(name) || name.startsWith(".") || name.endsWith(".")) {
    throw new TypeError("Volume name must be 1-128 ASCII letters, digits, '_', '-' or interior '.'");
  }
}

function validatePath(path: string): void {
  if (!path.startsWith("/") || path === "/" || path.includes("..") || path.includes("\0") ||
      new TextEncoder().encode(path).length > 256 || path.slice(1).split("/").some(p => p === "" || p === ".")) {
    throw new TypeError("Mount path must be canonical, absolute, non-root, at most 256 bytes, and contain no '..'");
  }
}

const volumeClients = new WeakMap<ArkerVolume, FilesystemClient>();

/** Explicit Arker identity; native handles and their tokens are never converted. */
export class ArkerVolume {
  constructor(readonly volumeId: string, readonly name: string, client?: FilesystemClient) {
    if (client) volumeClients.set(this, client);
  }

  async getInfo(_path: string, _opts?: Record<string, unknown>): Promise<never> {
    throw new Error("Volume.getInfo(path) requires the E2B content API; use sandbox.files");
  }

  async list(_path: string, _opts?: Record<string, unknown>): Promise<never> {
    throw new Error("Volume.list(path) requires the E2B content API; use sandbox.files");
  }

  async read(_path: string, _opts?: Record<string, unknown>): Promise<never> {
    throw new Error("Volume.read requires the E2B content API; use sandbox.files");
  }

  async write(_path: string, _data: unknown, _opts?: Record<string, unknown>): Promise<never> {
    throw new Error("Volume.write requires the E2B content API; use sandbox.files");
  }

  get token(): string {
    throw new Error("Arker volumes do not issue E2B volume-content tokens");
  }
}

export type VolumeMounts = Readonly<Record<string, ArkerVolume | string>>;
type FilesystemClient = Pick<Arker, "createFilesystem" | "getFilesystem" | "listFilesystems" | "deleteFilesystem">;

export class Volumes {
  constructor(private readonly client: FilesystemClient) {}

  async create(name: string): Promise<ArkerVolume> {
    validateName(name);
    const info = await this.client.createFilesystem({ name });
    return new ArkerVolume(info.filesystem_id, info.name, this.client);
  }

  async connect(volumeId: string): Promise<ArkerVolume> {
    const info = await this.client.getFilesystem(volumeId);
    return new ArkerVolume(info.filesystem_id, info.name, this.client);
  }

  async getInfo(volumeId: string): Promise<ArkerVolume> {
    return this.connect(volumeId);
  }

  async list(): Promise<ArkerVolume[]> {
    const result: ArkerVolume[] = [];
    let cursor: string | undefined;
    const seen = new Set<string>();
    do {
      const page = await this.client.listFilesystems({ cursor });
      result.push(...page.filesystems.map(info => new ArkerVolume(info.filesystem_id, info.name, this.client)));
      cursor = page.next_cursor ?? undefined;
      if (cursor && seen.has(cursor)) throw new Error("Filesystem listing repeated a cursor");
      if (cursor) seen.add(cursor);
    } while (cursor);
    return result;
  }

  /** Never create on a miss: service listings may lag or be truncated. */
  async byName(name: string): Promise<ArkerVolume> {
    validateName(name);
    const matches = (await this.list()).filter(volume => volume.name === name);
    if (!matches.length) throw new Error("Volume name was not found in the filesystem listing; connect by Arker ID if known");
    if (matches.length !== 1) throw new Error("Volume name is ambiguous");
    return this.connect(matches[0].volumeId);
  }

  async destroy(volumeId: string): Promise<boolean> {
    try {
      return (await this.client.deleteFilesystem(volumeId)).deleted;
    } catch (error) {
      if (error && typeof error === "object" && "status" in error && error.status === 404) return false;
      throw error;
    }
  }

  async resolve(volumeMounts: VolumeMounts): Promise<Record<string, ArkerVolume>> {
    const entries = Object.entries(volumeMounts);
    for (const [path, volume] of entries) {
      validatePath(path);
      if (typeof volume === "string") validateName(volume);
      else if (!(volume instanceof ArkerVolume)) throw new TypeError("Native Volume handles must use the native backend");
      else if (volumeClients.has(volume) && !sameClient(volumeClients.get(volume)!, this.client)) {
        throw new Error("Volume belongs to another Arker client; explicitly connect its ID on the selected client");
      }
    }
    const resolved: Record<string, ArkerVolume> = Object.create(null);
    const ids = new Set<string>();
    for (const [path, volume] of entries) {
      const handle = typeof volume === "string" ? await this.byName(volume) : await this.connect(volume.volumeId);
      if (ids.has(handle.volumeId)) throw new Error("A filesystem can only be mounted once per VM");
      ids.add(handle.volumeId);
      resolved[path] = handle;
    }
    const paths = Object.keys(resolved);
    if (paths.some(a => paths.some(b => a !== b && b.startsWith(a + "/")))) {
      throw new Error("Overlapping mount paths are unsupported");
    }
    return resolved;
  }

  /** Caller owns VM cleanup on partial failure. Never detach pre-existing mounts.
   * A successful response does not certify guest mount readiness.
   */
  async attach(vm: Pick<VM, "createMount">, volumeMounts: VolumeMounts) {
    const resolved = await this.resolve(volumeMounts);
    const mounts = [];
    for (const [path, volume] of Object.entries(resolved)) {
      mounts.push(await vm.createMount({ filesystemId: volume.volumeId, path }));
    }
    return mounts;
  }
}


function volumeOptions(options?: Record<string, unknown>): void {
  if (options && Object.keys(options).length) {
    throw new Error("Configure Arker through the arker option; per-call E2B connection options are unsupported");
  }
}

function sameClient(left: FilesystemClient, right: FilesystemClient): boolean {
  return left === right || (left instanceof Arker && right instanceof Arker &&
    left.baseUrl === right.baseUrl && left.controlBaseUrl === right.controlBaseUrl &&
    Reflect.get(left, "apiKey") === Reflect.get(right, "apiKey"));
}

export type VolumeOptions = ConnectionOpts & { routerConfigPath?: string; arker?: ArkerOptions };

function volumeRouter(options: VolumeOptions = {}) {
  const { routerConfigPath, arker, ...nativeOptions } = options;
  const native = {
    name: "e2b",
    instances: {
      create: (name: unknown) => NativeVolume.create(name as string, nativeOptions),
      getById: async (id: string) => {
        try { return await NativeVolume.connect(id, nativeOptions); }
        catch (error) { if (error instanceof VolumeNotFoundError) return null; throw error; }
      },
      destroy: async (id: string) => { await NativeVolume.destroy(id, nativeOptions); },
      list: () => NativeVolume.list(nativeOptions),
    },
  };
  function store() {
    volumeOptions(nativeOptions);
    return new Volumes(new Arker(arker));
  }
  const arkerProvider = {
    name: "arker",
    instances: {
      create: (name: unknown) => store().create(name as string),
      getById: async (id: string) => {
        try { return await store().connect(id); }
        catch (error) {
          if (error && typeof error === "object" && "status" in error && error.status === 404) return null;
          throw error;
        }
      },
      destroy: async (id: string) => { await store().destroy(id); },
      list: () => store().list(),
    },
  };
  const hasArker = !!(arker?.apiKey || globalThis.process?.env?.ARKER_API_KEY?.trim());
  const router = createRoutedProvider(
    [...(hasArker ? [arkerProvider] : []), native],
    routerConfigPath ?? globalThis.process?.env?.COMPUTESDK_ROUTER_CONFIG,
    { namespace: "e2b-volume", identifier: volume => volume.volumeId },
  );
  return { router, store, nativeOptions };
}

/** Lifecycle follows the shared routing policy; volume IDs have their own cache namespace. */
export class Volume {

  static async create(name: string, opts?: VolumeOptions): Promise<ArkerVolume | NativeVolume> {
    return volumeRouter(opts).router.instances!.create(name);
  }

  static async connect(volumeId: string, opts?: VolumeOptions): Promise<ArkerVolume | NativeVolume> {
    const found = await volumeRouter(opts).router.instances!.getById(volumeId);
    if (!found) throw new VolumeNotFoundError("Volume not found");
    return found;
  }

  static async getInfo(volumeId: string, opts?: VolumeOptions) {
    const { router, nativeOptions } = volumeRouter(opts);
    const owner = await router.getOwner(volumeId);
    if (!owner) throw new VolumeNotFoundError("Volume not found");
    return owner.provider === "arker" ? owner.instance as ArkerVolume : NativeVolume.getInfo(volumeId, nativeOptions);
  }

  static async list(opts?: VolumeOptions) {
    return volumeRouter(opts).router.list();
  }

  static async destroy(volumeId: string, opts?: VolumeOptions): Promise<boolean> {
    const { router, store, nativeOptions } = volumeRouter(opts);
    const owner = await router.getOwner(volumeId);
    if (!owner) return false;
    return owner.provider === "arker" ? store().destroy(volumeId) : NativeVolume.destroy(volumeId, nativeOptions);
  }
}

export type E2BVolumeMounts = Readonly<Record<string, ArkerVolume | NativeVolume | string>>;

export function checkMountBackend(mounts: E2BVolumeMounts | undefined, provider: "arker" | "e2b"): void {
  if (mounts === undefined) return;
  if (!mounts || typeof mounts !== "object" || Array.isArray(mounts)) throw new TypeError("volumeMounts must map paths to volumes or names");
  for (const [path, volume] of Object.entries(mounts)) {
    if (provider === "arker") {
      validatePath(path);
      if (typeof volume === "string") validateName(volume);
      else if (!(volume instanceof ArkerVolume)) throw new Error("Native Volume handles require the native E2B sandbox backend");
    } else if (volume instanceof ArkerVolume) {
      throw new Error("Arker Volume handles require the Arker sandbox backend");
    } else if (typeof volume !== "string" && !(volume instanceof NativeVolume)) {
      throw new TypeError("Expected a volume name or native Volume handle");
    }
  }
}

export function mountStore(client: FilesystemClient, mounts: E2BVolumeMounts): Volumes {
  const bound = Object.values(mounts).flatMap(volume => {
    const owner = volume instanceof ArkerVolume ? volumeClients.get(volume) : undefined;
    return owner ? [owner] : [];
  });
  if (bound.some(owner => !sameClient(owner, bound[0]))) throw new Error("Volume handles belong to different Arker clients");
  return new Volumes(bound[0] ?? client);
}

// Inspect the kernel mount table before returning a usable sandbox.

export function mountReadyCommand(paths: string[], timeout: number): string {
  return "python3 -I -c " + shellQuote(mountReadyScript) + " " + [String(timeout), ...paths].map(shellQuote).join(" ");
}
