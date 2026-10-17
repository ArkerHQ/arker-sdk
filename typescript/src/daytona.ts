import { daytona as createDaytonaProvider } from "@computesdk/daytona";
import { ArkerError } from "./index.js";

import { assertSupportedObjectKeys, guardUnsupported, nativeInstance, type UniversalSandbox } from "./compat/common.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";

export interface DaytonaCompatConfig {
  /** Daytona API key used only for native Daytona fallback. */
  apiKey?: string;
  /** Arker provider options. Falls back to ARKER_* environment variables. */
  arker?: ArkerComputeProviderConfig;
  routerConfigPath?: string;
}

export interface ExecuteResponse {
  exitCode: number;
  /** Daytona returns stdout as `result`. */
  result: string;
  stdout: string;
  stderr: string;
}

export interface ExecuteCommandOptions {
  cwd?: string;
  env?: Record<string, string>;
  timeout?: number;
}

const DAYTONA_CONFIG_KEYS = ["apiKey", "arker", "routerConfigPath"] as const;
const DAYTONA_CREATE_OPTION_KEYS = ["image", "snapshot", "envVars", "name", "language", "resources", "networkBlockAll", "networkAllowList", "domainAllowList"] as const;

function arkerCreateOptions(options: Record<string, unknown>): Record<string, unknown> {
  if (options.image !== undefined && typeof options.image !== "string") {
    throw new ArkerError("unsupported_operation", "Daytona Image builders are not supported on Arker; use a published image reference or route to Daytona", 422);
  }
  const { resources, ...rest } = options;
  if (resources === undefined) return rest;
  if (typeof resources !== "object" || resources === null || Array.isArray(resources)) throw new TypeError("Daytona resources must be an object");
  assertSupportedObjectKeys(resources, ["cpu", "memory", "disk"], "daytona", "resources");
  const converted: Record<string, number> = {};
  for (const [source, target, multiplier] of [["cpu", "cpu", 1], ["memory", "memoryMiB", 1024], ["disk", "diskMiB", 1024]] as const) {
    const value = (resources as Record<string, unknown>)[source];
    if (value === undefined) continue;
    if (typeof value !== "number" || value <= 0 || !Number.isSafeInteger(value * multiplier)) throw new TypeError(`Invalid Daytona resource ${source}`);
    converted[target] = value * multiplier;
  }
  return { ...rest, ...converted };
}

export class Sandbox {
  readonly id: string;
  readonly provider: string;
  #box: UniversalSandbox;

  readonly process: {
    exec: (command: string, options?: ExecuteCommandOptions) => Promise<ExecuteResponse>;
    executeCommand: (
      command: string,
      cwd?: string,
      env?: Record<string, string>,
      timeout?: number,
    ) => Promise<ExecuteResponse>;
  };

  constructor(box: UniversalSandbox) {
    this.#box = box;
    this.id = box.sandboxId;
    this.provider = box.provider;
    const run = async (command: string, options?: ExecuteCommandOptions): Promise<ExecuteResponse> => {
      const result = await this.#box.runCommand(command, {
        ...options,
        ...(options?.timeout !== undefined ? { timeout: options.timeout * 1_000 } : {}),
        ...(this.provider === "arker" && options?.cwd === undefined ? { cwd: "/home/daytona" } : {}),
      });
      return {
        exitCode: result.exitCode,
        result: result.stdout,
        stdout: result.stdout,
        stderr: result.stderr,
      };
    };
    this.process = {
      exec: run,
      executeCommand: (command, cwd, env, timeout) => run(command, { cwd, env, timeout }),
    };
  }

  async delete(): Promise<void> {
    await this.#box.destroy();
  }
}

export class Daytona {
  #sdk: CompatSdk;

  constructor(config?: DaytonaCompatConfig) {
    assertSupportedObjectKeys(config, DAYTONA_CONFIG_KEYS, "daytona", "new Daytona");
    this.#sdk = createCompatSdk({
      arker: config?.arker,
      fallbackProvider: createDaytonaProvider({ apiKey: config?.apiKey }),
      routerConfigPath: config?.routerConfigPath,
      mapArkerCreateOptions: arkerCreateOptions,
    });
    return guardUnsupported(this, "daytona");
  }

  async create(params?: Record<string, unknown>): Promise<Sandbox> {
    assertSupportedObjectKeys(params, DAYTONA_CREATE_OPTION_KEYS, "daytona", "Daytona.create");
    const source = params ?? {};
    const request = {
      ...(source.image !== undefined ? { image: source.image } : {}),
      ...(typeof source.snapshot === "string" ? { snapshotId: source.snapshot } : {}),
      ...(source.envVars && typeof source.envVars === "object" ? { envs: source.envVars as Record<string, string> } : {}),
      ...(typeof source.name === "string" ? { name: source.name } : {}),
      ...(typeof source.language === "string" ? { runtime: source.language } : {}),
      ...(source.resources !== undefined ? { resources: source.resources } : {}),
      ...(typeof source.networkBlockAll === "boolean" ? { networkBlockAll: source.networkBlockAll } : {}),
      ...(typeof source.networkAllowList === "string" ? { networkAllowList: source.networkAllowList } : {}),
      ...(typeof source.domainAllowList === "string" ? { domainAllowList: source.domainAllowList } : {}),
    };
    // ComputeSDK currently narrows `resources` to Vercel's shape; the native
    // Daytona provider forwards its own SDK's resources without that narrowing.
    const box = await this.#sdk.sandbox.create(request as Parameters<CompatSdk["sandbox"]["create"]>[0]);
    if (box.provider === "arker") await box.filesystem.mkdir("/home/daytona");
    return guardUnsupported(new Sandbox(box), "daytona", nativeInstance(box));
  }

  async get(sandboxId: string): Promise<Sandbox> {
    const box = await this.#sdk.sandbox.getById(sandboxId);
    if (!box) throw new Error(`Sandbox ${sandboxId} not found`);
    return guardUnsupported(new Sandbox(box), "daytona", nativeInstance(box));
  }

  async delete(sandbox: string | Sandbox): Promise<void> {
    const target = typeof sandbox === "string" ? await this.get(sandbox) : sandbox;
    await target.delete();
  }
}
export * from "@daytona/sdk";
