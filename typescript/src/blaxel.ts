import { blaxel as createBlaxelProvider } from "@computesdk/blaxel";

import { guardUnsupported, type UniversalSandbox } from "./compat/common.js";
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
}

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
  readonly metadata: { name: string };
  #box: UniversalSandbox;

  readonly process: {
    exec: (request: string | { command: string; workingDir?: string; env?: Record<string, string>; timeout?: number }) => Promise<{
      stdout: string;
      stderr: string;
      exitCode: number;
    }>;
  };

  readonly fs: {
    read: (path: string) => Promise<string>;
    write: (path: string, content: string) => Promise<void>;
    mkdir: (path: string) => Promise<void>;
    ls: (path: string) => Promise<{ files: unknown[]; subdirectories: unknown[] }>;
    rm: (path: string) => Promise<void>;
  };

  private constructor(box: UniversalSandbox) {
    this.#box = box;
    this.id = box.sandboxId;
    this.provider = box.provider;
    this.metadata = { name: box.sandboxId };
    this.process = {
      exec: async (request) => {
        const value = typeof request === "string" ? { command: request } : request;
        const result = await box.runCommand(value.command, {
          ...(value.workingDir ? { cwd: value.workingDir } : {}),
          ...(value.env ? { env: value.env } : {}),
          ...(value.timeout ? { timeout: value.timeout } : {}),
        });
        return { stdout: result.stdout, stderr: result.stderr, exitCode: result.exitCode };
      },
    };
    this.fs = {
      read: (path) => box.filesystem.readFile(path),
      write: (path, content) => box.filesystem.writeFile(path, content),
      mkdir: (path) => box.filesystem.mkdir(path),
      ls: async (path) => {
        const entries = await box.filesystem.readdir(path);
        return {
          files: entries.filter((entry: any) => entry.type === "file"),
          subdirectories: entries.filter((entry: any) => entry.type === "directory"),
        };
      },
      rm: (path) => box.filesystem.remove(path),
    };
    return guardUnsupported(this, "blaxel");
  }

  static async create(options: BlaxelCreateOptions = {}, config: BlaxelCompatConfig = {}): Promise<SandboxInstance> {
    const box = await makeSdk(config).sandbox.create({
      name: options.name,
      image: options.image,
      memory: options.memory,
      timeout: parseTtl(options.ttl),
      envs: options.envs,
      runtime: options.runtime,
    });
    return new SandboxInstance(box);
  }

  static async createIfNotExists(options: BlaxelCreateOptions = {}, config: BlaxelCompatConfig = {}): Promise<SandboxInstance> {
    void options;
    void config;
    throw new Error(
      "createIfNotExists cannot be implemented atomically across routed providers; " +
      "use create(), or the native Blaxel SDK when atomic name creation is required",
    );
  }

  static async get(id: string, config: BlaxelCompatConfig = {}): Promise<SandboxInstance> {
    const box = await makeSdk(config).sandbox.getById(id);
    if (!box) throw new Error(`Sandbox ${id} not found`);
    return new SandboxInstance(box);
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
