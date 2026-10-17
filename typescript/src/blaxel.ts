import { SandboxInstance as NativeBlaxelSandbox, settings as blaxelSettings } from "@blaxel/core";
import { chmod, readFile, writeFile } from "node:fs/promises";
import type { VM } from "./index.js";

import { assertSupportedObjectKeys, CompatibilityNotFoundError, guardUnsupported, resolveCompatPath, shellQuote, type UniversalSandbox } from "./compat/common.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";
import { GuestProcesses } from "./compat/guest-process.js";
import { privateFetch } from "./compat/private-fetch.js";
import { createNativeBlaxelProvider } from "./compat/blaxel-provider.js";
import { nativeInstance } from "./compat/common.js";

export interface BlaxelCompatConfig {
  apiKey?: string;
  workspace?: string;
  arker?: ArkerComputeProviderConfig;
  routerConfigPath?: string;
}

export interface BlaxelCreateOptions {
  name?: string;
  image?: string;
  memory?: number;
  ttl?: string | number;
  envs?: Record<string, string>;
  runtime?: string;
  region?: string;
  ports?: Array<{ target: number; protocol?: string }>;
  labels?: Record<string, string>;
  network?: NonNullable<NativeBlaxelSandbox["spec"]>["network"];
  safe?: boolean;
  timeout?: number;
}

export interface BlaxelCreateCallOptions extends BlaxelCompatConfig {
  safe?: boolean;
  createIfNotExist?: boolean;
  timeout?: number;
}

type BlaxelProcessRequest = Omit<Parameters<NativeBlaxelSandbox["process"]["exec"]>[0], "command"> & {
  command: string | string[];
  working_dir?: string;
  envs?: Record<string, string>;
};

type BlaxelCompatProcess = Omit<NativeBlaxelSandbox["process"], "exec"> & {
  exec(request: string | BlaxelProcessRequest): Promise<any>;
};

type BlaxelCompatFileSystem = Omit<NativeBlaxelSandbox["fs"], "write" | "mkdir" | "rm"> & {
  write(path: string, content: string | Uint8Array): Promise<any>;
  mkdir(path: string): Promise<any>;
  rm(path: string, recursive?: boolean): Promise<any>;
};

function parseTtl(ttl: string | number | undefined): number | undefined {
  if (ttl === undefined || typeof ttl === "number") return ttl;
  const match = /^(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)$/i.exec(ttl.trim());
  if (!match) throw new Error(`Invalid Blaxel ttl '${ttl}'; use milliseconds or a duration such as 30s, 5m, 2h, or 1d`);
  const multiplier = { ms: 1, s: 1_000, m: 60_000, h: 3_600_000, d: 86_400_000 }[match[2]!.toLowerCase()]!;
  return Number(match[1]) * multiplier;
}

function makeSdk(config: BlaxelCompatConfig = {}): CompatSdk {
  return createCompatSdk({
    arker: config.arker,
    fallbackProvider: createNativeBlaxelProvider({
      apiKey: config.apiKey,
      workspace: config.workspace,
    }),
    routerConfigPath: config.routerConfigPath,
  });
}

interface FetchProfile {
  region?: string;
  disableH2: boolean;
}

function fetchProfile(region?: string): FetchProfile {
  return { region: region || blaxelSettings.region, disableH2: blaxelSettings.disableH2 };
}

export class SandboxInstance {
  readonly id: string;
  readonly provider: string;
  readonly metadata: NativeBlaxelSandbox["metadata"];
  readonly network: NativeBlaxelSandbox["network"];
  declare readonly previews: NativeBlaxelSandbox["previews"];
  declare readonly schedules: NativeBlaxelSandbox["schedules"];
  declare readonly sessions: NativeBlaxelSandbox["sessions"];
  declare readonly codegen: NativeBlaxelSandbox["codegen"];
  declare readonly system: NativeBlaxelSandbox["system"];
  declare readonly drives: NativeBlaxelSandbox["drives"];
  declare readonly snapshots: NativeBlaxelSandbox["snapshots"];
  declare readonly status: NativeBlaxelSandbox["status"];
  declare readonly events: NativeBlaxelSandbox["events"];
  declare readonly spec: NativeBlaxelSandbox["spec"];
  declare readonly lastUsedAt: NativeBlaxelSandbox["lastUsedAt"];
  declare readonly errors: NativeBlaxelSandbox["errors"];
  declare readonly h2Domain: NativeBlaxelSandbox["h2Domain"];
  declare readonly expiresIn: NativeBlaxelSandbox["expiresIn"];
  declare readonly snapshot: NativeBlaxelSandbox["snapshot"];
  declare readonly listSnapshots: NativeBlaxelSandbox["listSnapshots"];
  declare readonly deleteSnapshot: NativeBlaxelSandbox["deleteSnapshot"];
  declare readonly restore: NativeBlaxelSandbox["restore"];
  declare readonly fork: NativeBlaxelSandbox["fork"];
  declare readonly wait: NativeBlaxelSandbox["wait"];
  declare readonly archive: NativeBlaxelSandbox["archive"];
  declare readonly unarchive: NativeBlaxelSandbox["unarchive"];
  #box: UniversalSandbox;
  #native?: NativeBlaxelSandbox;
  #h2Eligible: boolean;

  readonly process: BlaxelCompatProcess;
  readonly fs: BlaxelCompatFileSystem;

  private constructor(box: UniversalSandbox, native?: NativeBlaxelSandbox, profile = fetchProfile()) {
    this.#box = box;
    this.#native = native;
    this.#h2Eligible = Boolean(profile.region) && !profile.disableH2;
    const network = guardUnsupported({ fetch: this.fetch.bind(this) }, "blaxel");
    this.network = native?.network ?? network as unknown as NativeBlaxelSandbox["network"];
    this.id = box.sandboxId;
    this.provider = box.provider;
    const vm = box.provider === "arker" ? (box as UniversalSandbox & { getInstance(): VM }).getInstance() : undefined;
    this.metadata = (native?.metadata ?? { name: vm?.name ?? box.sandboxId }) as NativeBlaxelSandbox["metadata"];
    const guest = new GuestProcesses(box);
    this.process = (native?.process ?? {
      exec: async (request) => {
        const value = typeof request === "string" ? { command: request } : request;
        assertSupportedObjectKeys(value, ["command", "workingDir", "working_dir", "env", "envs", "timeout", "waitForCompletion", "name", "stdin", "onStdout", "onStderr", "onLog"], "blaxel", "process.exec");
        const command = Array.isArray(value.command)
          ? value.command.map((part) => `'${part.replaceAll("'", "'\\''")}'`).join(" ")
          : value.command;
        const { working_dir, envs, ...options } = value;
        return guest.exec({
          ...options,
          command,
          ...(value.timeout !== undefined ? { timeout: value.timeout * 1000 } : {}),
          workingDir: value.workingDir ?? working_dir,
          env: value.env ?? envs,
          waitForCompletion: typeof request === "string" ? true : value.waitForCompletion,
        });
      },
      get: guest.get.bind(guest),
      list: guest.list.bind(guest),
      wait: guest.wait.bind(guest),
      kill: guest.kill.bind(guest),
      stop: guest.stop.bind(guest),
      logs: guest.logs.bind(guest),
      writeStdin: guest.writeStdin.bind(guest),
      closeStdin: guest.closeStdin.bind(guest),
    }) as BlaxelCompatProcess;
    this.fs = (native?.fs ?? {
      read: (path) => box.filesystem.readFile(this.provider === "arker" ? resolveCompatPath(path, "/blaxel") : path),
      write: (path, content) => {
        if (typeof content !== "string") throw new TypeError("fs.write requires text; use fs.writeBinary for binary content");
        const resolved = resolveCompatPath(path, "/blaxel");
        return box.filesystem.writeFile(resolved, content).then(() => ({ message: "File created/updated successfully", path: resolved }));
      },
      writeBinary: async (path, content) => {
        const bytes = typeof content === "string" ? await readFile(content)
          : content instanceof Uint8Array ? content
          : new Uint8Array(await content.arrayBuffer());
        const resolved = resolveCompatPath(path, "/blaxel");
        const vm = (box as unknown as { getInstance(): VM }).getInstance();
        await vm.sync(resolved, bytes);
        return { message: "Binary file uploaded successfully", path: resolved };
      },
      readBinary: async (path) => {
        const vm = (box as unknown as { getInstance(): VM }).getInstance();
        const bytes = await vm.sync(resolveCompatPath(path, "/blaxel"));
        return new Blob([new Uint8Array(bytes)]);
      },
      download: async (src, destinationPath, { mode = 0o644 } = {}) => {
        const vm = (box as unknown as { getInstance(): VM }).getInstance();
        const bytes = await vm.sync(resolveCompatPath(src, "/blaxel"));
        await writeFile(destinationPath, bytes);
        await chmod(destinationPath, mode);
      },
      mkdir: async (path) => {
        const resolved = resolveCompatPath(path, "/blaxel");
        await box.filesystem.mkdir(resolved);
        return { message: "Directory created successfully", path: resolved };
      },
      ls: async (path) => {
        const entries = await box.filesystem.readdir(this.provider === "arker" ? resolveCompatPath(path, "/blaxel") : path);
        return {
          files: entries.filter((entry: any) => entry.type === "file"),
          subdirectories: entries.filter((entry: any) => entry.type === "directory"),
        };
      },
      rm: async (path, recursive = false) => {
        const resolved = resolveCompatPath(path, "/blaxel");
        const quoted = shellQuote(resolved);
        const directoryCommand = recursive ? "rm -r" : "rmdir";
        const result = await box.runCommand(
          `if [ -d ${quoted} ] && [ ! -L ${quoted} ]; then ` +
          `${directoryCommand} -- ${quoted} && printf directory; else rm -- ${quoted} && printf file; fi`,
        );
        if (result.exitCode !== 0) throw new Error(result.stderr || `remove failed with exit ${result.exitCode}`);
        return { message: `${result.stdout === "directory" ? "Directory" : "File"} deleted successfully`, path: resolved };
      },
    }) as BlaxelCompatFileSystem;
    return guardUnsupported(this, "blaxel", native);
  }

  async fetch(port: number, path = "/", init?: RequestInit): Promise<Response> {
    if (this.#native) return this.#native.fetch(port, path, init);
    const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
    return privateFetch(vm, port, path, init, this.#h2Eligible && !blaxelSettings.disableH2 ? "blaxel-h2" : "fetch");
  }

  private static async wrap(box: UniversalSandbox, profile?: FetchProfile): Promise<SandboxInstance> {
    if (box.provider !== "blaxel") return new SandboxInstance(box, undefined, profile);
    return new SandboxInstance(box, nativeInstance(box) as NativeBlaxelSandbox);
  }

  static async create(options: BlaxelCreateOptions = {}, callOptions: BlaxelCreateCallOptions = {}): Promise<SandboxInstance> {
    const profile = fetchProfile(options.region);
    const box = await makeSdk(callOptions).sandbox.create({
      name: options.name,
      image: options.image,
      memory: options.memory,
      timeout: parseTtl(options.ttl),
      envs: options.envs,
      runtime: options.runtime,
      ...(options.region !== undefined ? { region: options.region } : {}),
      ...(options.ports !== undefined ? { ports: options.ports } : {}),
      ...(options.labels !== undefined ? { labels: options.labels } : {}),
      ...(options.network !== undefined ? { network: options.network } : {}),
      ...((callOptions.safe ?? options.safe) !== undefined ? { safe: callOptions.safe ?? options.safe } : {}),
      createIfNotExist: callOptions.createIfNotExist ?? false,
      ...((callOptions.timeout ?? options.timeout) !== undefined
        ? { creationTimeout: callOptions.timeout ?? options.timeout }
        : {}),
    });
    if (box.provider === "arker") {
      try { await box.filesystem.mkdir("/blaxel"); }
      catch (error) {
        await box.destroy();
        throw error;
      }
    }
    return SandboxInstance.wrap(box, profile);
  }

  static async createIfNotExists(
    options: BlaxelCreateOptions = {},
    callOptions: Omit<BlaxelCreateCallOptions, "createIfNotExist"> = {},
  ): Promise<SandboxInstance> {
    if (!options.name) throw new Error("createIfNotExists requires a Blaxel sandbox name");
    try {
      return await SandboxInstance.get(options.name, callOptions);
    } catch (error) {
      if (!(error instanceof CompatibilityNotFoundError)) throw error;
      return SandboxInstance.create(options, { ...callOptions, createIfNotExist: true });
    }
  }

  static async get(id: string, config: BlaxelCompatConfig = {}): Promise<SandboxInstance> {
    const box = await makeSdk(config).sandbox.getById(id);
    if (!box) throw new CompatibilityNotFoundError(`Sandbox ${id} not found`);
    return SandboxInstance.wrap(box);
  }

  static async delete(id: string, config: BlaxelCompatConfig = {}): Promise<void> {
    await makeSdk(config).sandbox.destroy(id);
  }

  async delete(): Promise<void> {
    await this.#box.destroy();
  }
}

export class Blaxel {
  constructor(private readonly config: BlaxelCompatConfig = {}) {}
  create(options: BlaxelCreateOptions = {}) { return SandboxInstance.create(options, this.config); }
  get(id: string) { return SandboxInstance.get(id, this.config); }
  delete(id: string) { return SandboxInstance.delete(id, this.config); }
}
export * from "@blaxel/core";
