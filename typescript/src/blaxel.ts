import { blaxel as createBlaxelProvider } from "@computesdk/blaxel";
import { SandboxInstance as NativeBlaxelSandbox } from "@blaxel/core";

import { assertSupportedObjectKeys, guardUnsupported, resolveCompatPath, type UniversalSandbox } from "./compat/common.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";

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
    fallbackProvider: createBlaxelProvider({
      apiKey: config.apiKey,
      workspace: config.workspace,
      image: undefined,
    }),
    routerConfigPath: config.routerConfigPath,
  });
}

export class SandboxInstance {
  readonly id: string;
  readonly provider: string;
  readonly metadata: NativeBlaxelSandbox["metadata"];
  declare readonly network: NativeBlaxelSandbox["network"];
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
  declare readonly fetch: NativeBlaxelSandbox["fetch"];
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

  readonly process: BlaxelCompatProcess;
  readonly fs: BlaxelCompatFileSystem;

  private constructor(box: UniversalSandbox, native?: NativeBlaxelSandbox) {
    this.#box = box;
    this.#native = native;
    this.id = box.sandboxId;
    this.provider = box.provider;
    this.metadata = (native?.metadata ?? { name: box.sandboxId }) as NativeBlaxelSandbox["metadata"];
    this.process = (native?.process ?? {
      exec: async (request) => {
        const value = typeof request === "string" ? { command: request } : request;
        assertSupportedObjectKeys(value, ["command", "workingDir", "working_dir", "env", "envs", "timeout", "waitForCompletion"], "blaxel", "process.exec");
        if (value.waitForCompletion === false) {
          throw new Error("Arker process.exec does not support waitForCompletion=false");
        }
        const command = Array.isArray(value.command)
          ? value.command.map((part) => `'${part.replaceAll("'", "'\\''")}'`).join(" ")
          : value.command;
        const result = await box.runCommand(command, {
          ...(value.workingDir || value.working_dir ? { cwd: value.workingDir ?? value.working_dir } : this.provider === "arker" ? { cwd: "/blaxel" } : {}),
          ...(value.env || value.envs ? { env: value.env ?? value.envs } : {}),
          ...(value.timeout ? { timeout: value.timeout } : {}),
        });
        return { stdout: result.stdout, stderr: result.stderr, exitCode: result.exitCode };
      },
    }) as BlaxelCompatProcess;
    this.fs = (native?.fs ?? {
      read: (path) => box.filesystem.readFile(this.provider === "arker" ? resolveCompatPath(path, "/blaxel") : path),
      write: (path, content) => {
        if (typeof content !== "string") throw new Error("Blaxel binary writes are unavailable on Arker; use fs.writeBinary on native Blaxel");
        return box.filesystem.writeFile(this.provider === "arker" ? resolveCompatPath(path, "/blaxel") : path, content);
      },
      mkdir: (path) => box.filesystem.mkdir(this.provider === "arker" ? resolveCompatPath(path, "/blaxel") : path),
      ls: async (path) => {
        const entries = await box.filesystem.readdir(this.provider === "arker" ? resolveCompatPath(path, "/blaxel") : path);
        return {
          files: entries.filter((entry: any) => entry.type === "file"),
          subdirectories: entries.filter((entry: any) => entry.type === "directory"),
        };
      },
      rm: (path) => box.filesystem.remove(this.provider === "arker" ? resolveCompatPath(path, "/blaxel") : path),
    }) as BlaxelCompatFileSystem;
    return guardUnsupported(this, "blaxel", native);
  }

  private static async wrap(box: UniversalSandbox): Promise<SandboxInstance> {
    if (box.provider !== "blaxel") return new SandboxInstance(box);
    return new SandboxInstance(box, await NativeBlaxelSandbox.get(box.sandboxId));
  }

  static async create(options: BlaxelCreateOptions = {}, callOptions: BlaxelCreateCallOptions = {}): Promise<SandboxInstance> {
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
      ...((callOptions.safe ?? options.safe) !== undefined ? { safe: callOptions.safe ?? options.safe } : {}),
      ...(callOptions.createIfNotExist !== undefined ? { createIfNotExist: callOptions.createIfNotExist } : {}),
      ...((callOptions.timeout ?? options.timeout) !== undefined
        ? { creationTimeout: callOptions.timeout ?? options.timeout }
        : {}),
    });
    if (box.provider === "arker") await box.filesystem.mkdir("/blaxel");
    return SandboxInstance.wrap(box);
  }

  static async createIfNotExists(
    options: BlaxelCreateOptions = {},
    callOptions: Omit<BlaxelCreateCallOptions, "createIfNotExist"> = {},
  ): Promise<SandboxInstance> {
    if (!options.name) throw new Error("createIfNotExists requires a Blaxel sandbox name");
    try {
      return await SandboxInstance.get(options.name, callOptions);
    } catch (error) {
      if (!(error instanceof Error) || !/not found/i.test(error.message)) throw error;
      return SandboxInstance.create(options, { ...callOptions, createIfNotExist: true });
    }
  }

  static async get(id: string, config: BlaxelCompatConfig = {}): Promise<SandboxInstance> {
    const box = await makeSdk(config).sandbox.getById(id);
    if (!box) throw new Error(`Sandbox ${id} not found`);
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
