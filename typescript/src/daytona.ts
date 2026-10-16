import { daytona as createDaytonaProvider } from "@computesdk/daytona";

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
    });
    return guardUnsupported(this, "daytona");
  }

  async create(params?: Record<string, unknown>): Promise<Sandbox> {
    assertSupportedObjectKeys(params, DAYTONA_CREATE_OPTION_KEYS, "daytona", "Daytona.create");
    const source = params ?? {};
    const box = await this.#sdk.sandbox.create({
      ...(typeof source.image === "string" ? { image: source.image } : {}),
      ...(typeof source.snapshot === "string" ? { snapshotId: source.snapshot } : {}),
      ...(source.envVars && typeof source.envVars === "object" ? { envs: source.envVars as Record<string, string> } : {}),
      ...(typeof source.name === "string" ? { name: source.name } : {}),
      ...(typeof source.language === "string" ? { runtime: source.language } : {}),
      ...(source.resources && typeof source.resources === "object" ? source.resources : {}),
      ...(typeof source.networkBlockAll === "boolean" ? { networkBlockAll: source.networkBlockAll } : {}),
      ...(typeof source.networkAllowList === "string" ? { networkAllowList: source.networkAllowList } : {}),
      ...(typeof source.domainAllowList === "string" ? { domainAllowList: source.domainAllowList } : {}),
    });
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
