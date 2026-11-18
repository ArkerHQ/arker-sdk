/**
 * Modal-compatible sandboxes backed by Arker VMs.
 *
 * Replace `from "modal"` with `from "@arker-ai/sdk/modal"`. What this layer
 * cannot honor throws rather than doing something else.
 */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import { parseDockerfile } from "./buildSpec.js";
import {
  CompatSandbox,
  type Egress,
  Volumes,
  guardUnsupported,
  importOptional,
  permissions,
  rejectOptions,
  shellQuote,
  unsupported,
  type CompatConfig,
  type Entry,
  type Errors,
  type Process,
} from "./compat/core.js";
import type { VM } from "./index.js";

const DEFAULT_TIMEOUT_MS = 300_000;
const WATCH_INTERVAL_MS = 500;

// Options Modal uses to reach its own service; they have no meaning on Arker.
const MODAL_CONNECTION_OPTIONS = new Set(["cloud", "regions", "verbose"]);

// Like Modal's, each error class stands alone: catching one never catches another.
class ModalError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = new.target.name;
  }
}
export class InvalidError extends ModalError {}
export class NotFoundError extends ModalError {}
export class ExecutionError extends ModalError {}
export class TimeoutError extends ModalError {}
export class NotSupportedError extends InvalidError {}
export class SandboxFilesystemError extends ModalError {}
export class SandboxFilesystemNotFoundError extends SandboxFilesystemError {}

const ERRORS: Errors = {
  provider: "Modal",
  unsupported: NotSupportedError,
  invalid: InvalidError,
  notFound: NotFoundError,
  fileNotFound: SandboxFilesystemNotFoundError,
  timeout: TimeoutError,
  failed: ExecutionError,
};

export class App {
  constructor(readonly name: string) {}
}

export class Secret {
  constructor(readonly env: Record<string, string>) {}
}

/**
 * A persistent volume, as an Arker filesystem, mounted with `sandboxes.create`'s
 * `volumes`. Writes are visible to every sandbox that mounts it at once.
 */
export class Volume {
  /** @internal */
  constructor(
    readonly volumeId: string,
    readonly name: string,
  ) {}
}

/** A build step: a shell command, run with `envs`. */
interface Step {
  command: string;
  envs: Readonly<Record<string, string>>;
}

export interface ImageDockerfileCommandsParams {
  env?: Record<string, string>;
  secrets?: Secret[];
  gpu?: string;
  forceBuild?: boolean;
}

/** A registry image (or the default VM) plus build steps run after the fork. */
export class Image {
  constructor(
    /** @internal */ readonly registry?: string,
    /** @internal */ readonly steps: readonly Step[] = [],
    /** @internal */ readonly env: Readonly<Record<string, string>> = {},
    /** @internal */ readonly workdir?: string,
    /** @internal The VM a filesystem snapshot was taken into. */ readonly snapshot?: VM,
  ) {}

  get imageId(): string | undefined {
    return this.snapshot?.id;
  }

  /** The image as it is: its build steps run when a sandbox is created from it. */
  async build(_app?: App): Promise<Image> {
    return this;
  }

  /** Apply `RUN`, `ENV` and `WORKDIR` instructions, in order; `env` and `secrets` apply to its `RUN`s. */
  dockerfileCommands(commands: string[], params: ImageDockerfileCommandsParams = {}): Image {
    const { env, secrets, gpu, forceBuild: _forceBuild, ...rest } = params;
    rejectOptions(ERRORS, "Image.dockerfileCommands", { gpu, ...rest });
    const envs = envFor(env, secrets);
    let image: Image = this;
    for (const step of parseDockerfile(`FROM scratch\n${commands.join("\n")}`).steps) {
      if (step.kind === "run") image = image.#then(step.command, envs);
      else if (step.kind === "env") {
        image = new Image(image.registry, image.steps, { ...image.env, ...Object.fromEntries(step.pairs) }, image.workdir, image.snapshot);
      } else if (step.kind === "workdir") {
        const created = image.#then(`mkdir -p ${shellQuote(step.path)}`, {});
        image = new Image(created.registry, created.steps, created.env, step.path, created.snapshot);
      } else {
        throw new InvalidError(`Dockerfile instruction '${step.kind.toUpperCase()}' is not supported on Arker.`);
      }
    }
    return image;
  }

  #then(command: string, envs: Record<string, string>): Image {
    const step = this.workdir ? `cd ${shellQuote(this.workdir)} && ${command}` : command;
    return new Image(this.registry, [...this.steps, { command: step, envs }], this.env, this.workdir, this.snapshot);
  }
}

export interface ModalReadStream<R = string> extends ReadableStream<R> {
  readText(): Promise<string>;
  readBytes(): Promise<Uint8Array>;
}

export interface ModalWriteStream<R = string> extends WritableStream<R> {
  writeText(text: string): Promise<void>;
  writeBytes(bytes: Uint8Array): Promise<void>;
}

/** `stdout` or `stderr` of a process: read it whole, or stream it as it arrives. */
function readStream(process: Process, index: 0 | 1, binary = false): ModalReadStream<any> {
  let chunks: AsyncGenerator<[string, string]> | undefined;
  const encoder = new TextEncoder();
  const stream = new ReadableStream<string | Uint8Array>(
    {
      async pull(controller) {
        chunks ??= process.follow();
        for (;;) {
          const next = await chunks.next();
          if (next.done) return controller.close();
          const text = next.value[index];
          if (text) return controller.enqueue(binary ? encoder.encode(text) : text);
        }
      },
    },
    // Pull only when read, so an unread stream never polls.
    { highWaterMark: 0 },
  );
  const readText = async () => {
    const result = await process.wait();
    return index === 0 ? result.stdout : result.stderr;
  };
  return Object.assign(stream, { readText, readBytes: async () => encoder.encode(await readText()) });
}

/** A process's stdin: each write is sent as it is made, and closing sends end of file. */
function writeStream(process: Process): ModalWriteStream<any> {
  const stream = new WritableStream<string | Uint8Array>({
    write: (chunk) => process.sendStdin(chunk),
    close: () => process.closeStdin(),
  });
  const write = async (chunk: string | Uint8Array) => {
    const writer = stream.getWriter();
    try {
      await writer.write(chunk);
    } finally {
      writer.releaseLock();
    }
  };
  return Object.assign(stream, { writeText: write, writeBytes: write });
}

export class ContainerProcess<R extends string | Uint8Array = any> {
  readonly stdin: ModalWriteStream<R>;
  readonly stdout: ModalReadStream<R>;
  readonly stderr: ModalReadStream<R>;

  constructor(
    private readonly process: Process,
    binary = false,
  ) {
    this.stdin = writeStream(process);
    this.stdout = readStream(process, 0, binary);
    this.stderr = readStream(process, 1, binary);
  }

  async closeStdin(): Promise<void> {
    await this.process.closeStdin();
  }

  async poll(): Promise<number | null> {
    return this.process.poll();
  }

  async wait(): Promise<number> {
    return (await this.process.wait()).exitCode;
  }
}

export class Tunnel {
  readonly port = 443;
  constructor(readonly host: string) {}

  get url(): string {
    return `https://${this.host}`;
  }

  get tlsSocket(): [string, number] {
    return [this.host, this.port];
  }
}

/** A file opened with `Sandbox.open`; writes land when it is flushed or closed. */
export class SandboxFile {
  #data: Uint8Array = new Uint8Array();

  private constructor(
    private readonly sandbox: CompatSandbox,
    private readonly path: string,
    private readonly writable: boolean,
  ) {}

  /** @internal */
  static async open(sandbox: CompatSandbox, path: string, mode: string): Promise<SandboxFile> {
    const file = new SandboxFile(sandbox, path, /[wa+]/.test(mode));
    if (mode.includes("r") || mode.includes("a")) {
      try {
        file.#data = await sandbox.read(path);
      } catch (error) {
        if (mode.includes("r") || !(error instanceof SandboxFilesystemNotFoundError)) throw error;
      }
    }
    return file;
  }

  async read(): Promise<Uint8Array> {
    return this.#data;
  }

  async write(data: Uint8Array | string): Promise<void> {
    if (!this.writable) throw new InvalidError("File is not open for writing.");
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    const joined = new Uint8Array(this.#data.length + bytes.length);
    joined.set(this.#data);
    joined.set(bytes, this.#data.length);
    this.#data = joined;
  }

  async flush(): Promise<void> {
    if (this.writable) await this.sandbox.write(this.path, this.#data);
  }

  async close(): Promise<void> {
    await this.flush();
  }
}

export interface SandboxExecParams {
  mode?: "text" | "binary";
  stdout?: "pipe" | "ignore";
  stderr?: "pipe" | "ignore";
  workdir?: string;
  timeoutMs?: number;
  env?: Record<string, string>;
  secrets?: Secret[];
  pty?: boolean;
  [option: string]: unknown;
}

export type FileType = "file" | "directory" | "symlink";

export interface FileInfo {
  readonly name: string;
  readonly path: string;
  readonly type: FileType;
  readonly size: number;
  readonly mode: number;
  readonly permissions: string;
  readonly owner: string;
  readonly group: string;
  /** Seconds since the epoch. */
  readonly modifiedTime: number;
  readonly symlinkTarget: string | null;
}

export type FileWatchEventType = "Access" | "Create" | "Modify" | "Remove" | "Unknown";

export interface FileWatchEvent {
  readonly eventType: FileWatchEventType;
  readonly paths: string[];
}

const WATCH_EVENT_TYPES: Record<string, FileWatchEventType> = { create: "Create", write: "Modify", chmod: "Modify", remove: "Remove" };

function fileInfo(entry: Entry): FileInfo {
  return {
    name: entry.name,
    path: entry.path,
    type: entry.symlinkTarget ? "symlink" : entry.isDir ? "directory" : "file",
    size: entry.size,
    mode: entry.mode,
    permissions: permissions(entry.mode),
    owner: entry.owner,
    group: entry.group,
    modifiedTime: entry.modified,
    symlinkTarget: entry.symlinkTarget ?? null,
  };
}

/** `sandbox.filesystem`: files in the sandbox, by absolute path. */
export class SandboxFilesystem {
  constructor(private readonly sandbox: CompatSandbox) {}

  async readBytes(remotePath: string): Promise<Uint8Array> {
    return this.sandbox.read(remotePath);
  }

  async readText(remotePath: string): Promise<string> {
    return new TextDecoder().decode(await this.sandbox.read(remotePath));
  }

  async writeBytes(data: Uint8Array | ArrayBuffer, remotePath: string): Promise<void> {
    await this.sandbox.write(remotePath, data instanceof Uint8Array ? data : new Uint8Array(data));
  }

  async writeText(data: string, remotePath: string): Promise<void> {
    await this.sandbox.write(remotePath, data);
  }

  async copyFromLocal(localPath: string, remotePath: string): Promise<void> {
    await this.sandbox.write(remotePath, await readFile(localPath));
  }

  async copyToLocal(remotePath: string, localPath: string): Promise<void> {
    const data = await this.sandbox.read(remotePath);
    await mkdir(dirname(localPath), { recursive: true });
    await writeFile(localPath, data);
  }

  async listFiles(remotePath: string): Promise<FileInfo[]> {
    return (await this.sandbox.list(remotePath)).map(fileInfo);
  }

  async stat(remotePath: string): Promise<FileInfo> {
    return fileInfo(await this.sandbox.stat(remotePath));
  }

  async makeDirectory(remotePath: string, options: { createParents?: boolean } = {}): Promise<void> {
    await this.sandbox.makeDir(remotePath, { parents: options.createParents ?? true });
  }

  async remove(remotePath: string, options: { recursive?: boolean } = {}): Promise<void> {
    await this.sandbox.remove(remotePath, { recursive: options.recursive ?? false });
  }

  /** Watch a directory by polling its listing; a rename reads as a remove and a create. */
  async *watch(
    remotePath: string,
    params: { filter?: FileWatchEventType[]; recursive?: boolean; timeoutMs?: number } = {},
  ): AsyncIterable<FileWatchEvent> {
    if (!(await this.sandbox.isDir(remotePath))) throw unsupported(ERRORS, "filesystem.watch on a file", "watch its directory.");
    const deadline = params.timeoutMs === undefined ? Infinity : Date.now() + params.timeoutMs;
    let [, entries] = await this.sandbox.changes(remotePath, new Map(), Boolean(params.recursive));
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, WATCH_INTERVAL_MS));
      const [changes, after] = await this.sandbox.changes(remotePath, entries, Boolean(params.recursive));
      entries = after;
      for (const change of changes) {
        const event: FileWatchEvent = { eventType: WATCH_EVENT_TYPES[change.kind]!, paths: [change.path.replace(/\/+/g, "/")] };
        if (!params.filter || params.filter.includes(event.eventType)) yield event;
      }
    }
  }
}

export interface SandboxCreateParams {
  cpu?: number;
  memoryMiB?: number;
  timeoutMs?: number;
  workdir?: string;
  command?: string[];
  env?: Record<string, string>;
  secrets?: Secret[];
  encryptedPorts?: number[];
  blockNetwork?: boolean;
  /** CIDR blocks the sandbox may reach; everything else is denied. */
  cidrAllowlist?: string[];
  outboundCidrAllowlist?: string[];
  /** Domains the sandbox may reach; everything else is denied. */
  outboundDomainAllowlist?: string[];
  /** CIDR blocks that may reach the sandbox's ports. */
  inboundCidrAllowlist?: string[];
  name?: string;
  tags?: Record<string, string>;
  volumes?: Record<string, Volume>;
  [option: string]: unknown;
}

export interface SandboxListParams {
  /** Arker sandboxes belong to no app, so this does not narrow the list. */
  appId?: string;
  tags?: Record<string, string>;
  [option: string]: unknown;
}

interface Settings {
  workdir?: string;
  ports?: number[];
  inbound?: string[];
  tags?: Record<string, string>;
}

/** Modal's network options: any allowlist denies what it does not name. */
function egressFor(blockNetwork: boolean | undefined, cidrs: string[] | undefined, domains: string[] | undefined): Egress | undefined {
  if (blockNetwork) return { denyAll: true };
  if (!cidrs && !domains) return undefined;
  return { allowIps: cidrs ?? [], allowHosts: domains ?? [], denyAll: true };
}

/** A Modal sandbox running as an Arker VM. Like Modal, it is deleted `timeoutMs` (default 300 000) after it is created. */
export class Sandbox {
  readonly sandboxId: string;
  #terminatedCode: number | undefined;
  readonly filesystem: SandboxFilesystem;
  readonly #streams: ContainerProcess<string> | undefined;

  /** @internal */
  constructor(
    private readonly sandbox: CompatSandbox,
    private readonly settings: Settings,
    private readonly entrypoint?: Process,
  ) {
    this.sandboxId = sandbox.id;
    this.filesystem = guardUnsupported(new SandboxFilesystem(sandbox), ERRORS, "Sandbox.filesystem");
    this.#streams = entrypoint ? new ContainerProcess(entrypoint) : undefined;
  }

  get stdin(): ModalWriteStream<string> {
    return this.#entrypointStreams().stdin;
  }

  get stdout(): ModalReadStream<string> {
    return this.#entrypointStreams().stdout;
  }

  get stderr(): ModalReadStream<string> {
    return this.#entrypointStreams().stderr;
  }

  #entrypointStreams(): ContainerProcess<string> {
    if (!this.#streams) throw new InvalidError("This sandbox has no command; pass one as `command` to sandboxes.create().");
    return this.#streams;
  }

  async setTags(tags: Record<string, string>): Promise<void> {
    await this.sandbox.updateMetadata({ tags });
  }

  async getTags(): Promise<Record<string, string>> {
    return (await this.sandbox.metadata()).tags ?? {};
  }

  /** An image of this sandbox's files (and memory) for `sandboxes.create`, deleted after `ttlMs` (30 days) unless it is null. */
  async snapshotFilesystem(params: { timeoutMs?: number; ttlMs?: number | null } = {}): Promise<Image> {
    const ttlMs = params.ttlMs === undefined ? 30 * 24 * 3_600_000 : params.ttlMs;
    const snapshot = await this.sandbox.snapshot(`modal-snapshot-${crypto.randomUUID().slice(0, 12)}`, ttlMs === null ? undefined : ttlMs / 1_000);
    return new Image(undefined, [], {}, undefined, snapshot.vm);
  }

  /** Volumes are Arker filesystems, which every sandbox sees written at once, so there is nothing to reload. */
  async reloadVolumes(_params?: { timeoutMs?: number }): Promise<void> {}

  /** A sandbox is ready once created, so this returns at once. */
  async waitUntilReady(timeoutMs = 300_000): Promise<void> {
    if (timeoutMs <= 0) throw new InvalidError(`\`timeoutMs\` must be positive, got: ${timeoutMs}`);
  }

  /** This layer holds no connection to a sandbox, so there is nothing to close. */
  detach(): void {}

  /** The Arker VM behind this sandbox, for anything this layer does not cover. */
  get arker(): VM {
    return this.sandbox.vm;
  }

  /** Start a command; it runs alongside other processes in the sandbox. */
  async exec(command: string[], params: SandboxExecParams = {}): Promise<ContainerProcess> {
    const { mode, stdout, stderr, workdir, timeoutMs, env, secrets, pty, ...rest } = params;
    rejectOptions(ERRORS, "Sandbox.exec", { ...rest, pty: pty || undefined });
    let script = command.map(shellQuote).join(" ");
    if (stdout === "ignore") script += " >/dev/null";
    if (stderr === "ignore") script += " 2>/dev/null";
    const process = await this.sandbox.start(script, {
      cwd: workdir ?? this.settings.workdir,
      envs: envFor(env, secrets),
      timeout: timeoutMs ? timeoutMs / 1_000 : undefined,
      stdin: true,
    });
    return guardUnsupported(new ContainerProcess(process, mode === "binary"), ERRORS, "ContainerProcess");
  }

  async open(path: string, mode = "r"): Promise<SandboxFile> {
    return SandboxFile.open(this.sandbox, path, mode);
  }

  async tunnels(): Promise<Record<number, Tunnel>> {
    const tunnels: Record<number, Tunnel> = {};
    for (const port of this.settings.ports ?? []) tunnels[port] = new Tunnel(await this.sandbox.expose(port, { fromIps: this.settings.inbound }));
    return tunnels;
  }

  async poll(): Promise<number | null> {
    if (this.#terminatedCode !== undefined) return this.#terminatedCode;
    return this.entrypoint ? this.entrypoint.poll() : null;
  }

  async wait(): Promise<number> {
    if (!this.entrypoint) {
      throw new InvalidError("This sandbox has no command to wait for; pass one as `command` to sandboxes.create().");
    }
    return (await this.entrypoint.wait()).exitCode;
  }

  /**
   * Delete the sandbox. With `wait`, return its exit code: the entrypoint's if
   * it had finished, else 137 as Modal reports a terminated sandbox.
   */
  async terminate(): Promise<void>;
  async terminate(params: { wait: true }): Promise<number>;
  async terminate(params: { wait?: boolean } = {}): Promise<number | void> {
    const finished = await this.poll();
    await this.sandbox.delete();
    this.#terminatedCode = finished ?? 137;
    if (params.wait) return this.#terminatedCode;
  }
}

function envFor(env: Record<string, string> | undefined, secrets: Secret[] | undefined): Record<string, string> {
  const merged = { ...env };
  for (const secret of secrets ?? []) {
    if (!(secret instanceof Secret)) throw new InvalidError("Only secrets from secrets.fromObject() are supported on Arker.");
    Object.assign(merged, secret.env);
  }
  return merged;
}

export interface ModalCompatConfig {
  /** Modal credentials, used only to reach existing Modal sandboxes by id. */
  tokenId?: string;
  tokenSecret?: string;
  /** Where and how to reach Arker, and which VM to start from. */
  arker?: CompatConfig;
}

export class ModalClient {
  constructor(private readonly config: ModalCompatConfig = {}) {
    return guardUnsupported(this, ERRORS, "ModalClient");
  }

  readonly apps = {
    fromName: async (name: string, _params?: Record<string, unknown>): Promise<App> => new App(name),
  };

  readonly secrets = {
    fromObject: async (entries: Record<string, string>): Promise<Secret> => new Secret({ ...entries }),
  };

  readonly images = {
    fromRegistry: (tag: string): Image => new Image(tag),

    /** The image of a sandbox's filesystem snapshot, by its `imageId`. */
    fromId: async (imageId: string): Promise<Image> => {
      const snapshot = await CompatSandbox.connect(ERRORS, imageId, this.config.arker);
      if (!snapshot || snapshot.snapshotOf === undefined) throw new NotFoundError(`Image ${imageId} not found`);
      return new Image(undefined, [], {}, undefined, snapshot.vm);
    },
  };

  readonly volumes = {
    fromName: async (name: string, params: { createIfMissing?: boolean; [option: string]: unknown } = {}): Promise<Volume> => {
      const { createIfMissing, environment: _environment, version: _version, ...rest } = params;
      rejectOptions(ERRORS, "volumes.fromName", rest);
      const volume = await new Volumes(ERRORS, this.config.arker).get(name, { create: createIfMissing });
      return new Volume(volume.filesystem_id, volume.name);
    },

    delete: async (name: string, params: { allowMissing?: boolean; [option: string]: unknown } = {}): Promise<void> => {
      const { allowMissing, environment: _environment, ...rest } = params;
      rejectOptions(ERRORS, "volumes.delete", rest);
      const volumes = new Volumes(ERRORS, this.config.arker);
      const found = await volumes.find(name);
      const deleted = found !== undefined && (await volumes.delete(found.filesystem_id));
      if (!deleted && !allowMissing) throw new NotFoundError(`Volume '${name}' not found`);
    },
  };

  readonly sandboxes = {
    create: async (_app?: App, image: Image = new Image(), params: SandboxCreateParams = {}): Promise<Sandbox> => {
      const {
        cpu,
        memoryMiB,
        timeoutMs,
        workdir,
        command,
        env,
        secrets,
        encryptedPorts = [],
        blockNetwork,
        cidrAllowlist,
        outboundCidrAllowlist,
        outboundDomainAllowlist,
        inboundCidrAllowlist,
        name,
        tags,
        volumes = {},
        ...rest
      } = params;
      rejectOptions(ERRORS, "sandboxes.create", rest, MODAL_CONNECTION_OPTIONS);
      if (cidrAllowlist && outboundCidrAllowlist) throw new InvalidError("Cannot specify both `cidrAllowlist` and `outboundCidrAllowlist`.");
      if (Object.values(volumes).some((volume) => !(volume instanceof Volume))) {
        throw new InvalidError("Only volumes from volumes.fromName() can be mounted on Arker.");
      }
      const settings: Settings = {
        workdir: workdir ?? image.workdir,
        ports: [...encryptedPorts],
        ...(inboundCidrAllowlist?.length ? { inbound: inboundCidrAllowlist } : {}),
        ...(tags ? { tags } : {}),
      };
      const sandbox = await CompatSandbox.create(ERRORS, {
        config: this.config.arker,
        image: image.registry,
        parent: image.snapshot,
        envs: { ...image.env, ...envFor(env, secrets) },
        cpus: cpu ? Math.ceil(cpu) : undefined,
        memoryMiB,
        deleteAfter: (timeoutMs || DEFAULT_TIMEOUT_MS) / 1_000,
        metadata: settings,
        name,
        volumes: Object.fromEntries(Object.entries(volumes).map(([path, volume]) => [path, volume.volumeId])),
      });
      try {
        for (const { command, envs } of image.steps) {
          const result = await sandbox.run(command, { envs: { ...envs } });
          if (result.exitCode !== 0) throw new ExecutionError(`Image build step failed (${result.exitCode}): ${command}\n${result.stderr}`);
        }
        const egress = egressFor(blockNetwork, outboundCidrAllowlist ?? cidrAllowlist, outboundDomainAllowlist);
        // After the build steps, which may need to download packages.
        if (egress) await sandbox.restrictEgress(egress);
        for (const port of encryptedPorts) await sandbox.expose(port, { fromIps: settings.inbound });
        const entrypoint = command?.length
          ? await sandbox.start(command.map(shellQuote).join(" "), { cwd: settings.workdir, stdin: true })
          : undefined;
        return guardUnsupported(new Sandbox(sandbox, settings, entrypoint), ERRORS, "Sandbox");
      } catch (error) {
        await sandbox.discard();
        throw error;
      }
    },

    /**
     * An id Arker does not know is handed to the `modal` package when it is
     * installed, so existing Modal sandboxes keep working.
     */
    fromId: async (sandboxId: string): Promise<Sandbox> => {
      const sandbox = await CompatSandbox.connect(ERRORS, sandboxId, this.config.arker);
      if (!sandbox) {
        const native = await importOptional<NativeModal>("modal");
        if (!native) throw new NotFoundError(`Sandbox ${sandboxId} not found`);
        const { tokenId, tokenSecret } = this.config;
        return new native.ModalClient({ tokenId, tokenSecret }).sandboxes.fromId(sandboxId);
      }
      return guardUnsupported(new Sandbox(sandbox, await sandbox.metadata()), ERRORS, "Sandbox");
    },

    /**
     * The sandbox named `name`; Arker names are unique in an organization, so
     * `appName` is not needed. A name Arker does not know is handed to the
     * `modal` package when it is installed.
     */
    fromName: async (appName: string, name: string, params?: Record<string, unknown>): Promise<Sandbox> => {
      const sandbox = await CompatSandbox.connect(ERRORS, name, this.config.arker);
      if (!sandbox) {
        const native = await importOptional<NativeModal>("modal");
        if (!native) throw new NotFoundError(`Sandbox '${name}' not found in app '${appName}'`);
        const { tokenId, tokenSecret } = this.config;
        return new native.ModalClient({ tokenId, tokenSecret }).sandboxes.fromName(appName, name, params);
      }
      return guardUnsupported(new Sandbox(sandbox, await sandbox.metadata()), ERRORS, "Sandbox");
    },

    /** The sandboxes this layer created that have at least `tags`. */
    list: (params: SandboxListParams = {}): AsyncGenerator<Sandbox> => listSandboxes(this.config.arker, params),
  };
}

async function* listSandboxes(config: CompatConfig | undefined, params: SandboxListParams): AsyncGenerator<Sandbox> {
  const { appId, tags = {}, ...rest } = params;
  rejectOptions(ERRORS, "sandboxes.list", { appId, ...rest }, new Set(["environment"]), new Set(["appId"]));
  for await (const sandbox of CompatSandbox.find(ERRORS, config)) {
    const own: Record<string, string> = sandbox.settings.tags ?? {};
    if (Object.entries(tags).every(([key, value]) => own[key] === value)) {
      yield guardUnsupported(new Sandbox(sandbox, sandbox.settings), ERRORS, "Sandbox");
    }
  }
}

interface NativeModal {
  ModalClient: new (params?: Record<string, unknown>) => {
    sandboxes: {
      fromId(sandboxId: string): Promise<Sandbox>;
      fromName(appName: string, name: string, params?: Record<string, unknown>): Promise<Sandbox>;
    };
  };
}
