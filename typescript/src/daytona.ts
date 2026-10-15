import { daytona as createDaytonaProvider } from "@computesdk/daytona";

import { assertSupportedObjectKeys, guardUnsupported, type UniversalSandbox } from "./compat/common.js";
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

const DAYTONA_CONFIG_KEYS = ["apiKey", "arker", "routerConfigPath"] as const;
const DAYTONA_CREATE_OPTION_KEYS = ["image", "snapshot", "envVars", "name", "language", "resources"] as const;

export class Sandbox {
  readonly id: string;
  readonly provider: string;
  #box: UniversalSandbox;

  readonly process: {
    exec: (command: string) => Promise<ExecuteResponse>;
    executeCommand: (command: string) => Promise<ExecuteResponse>;
  };

  constructor(box: UniversalSandbox) {
    this.#box = box;
    this.id = box.sandboxId;
    this.provider = box.provider;
    const run = async (command: string): Promise<ExecuteResponse> => {
      const result = await this.#box.runCommand(command);
      return {
        exitCode: result.exitCode,
        result: result.stdout,
        stdout: result.stdout,
        stderr: result.stderr,
      };
    };
    this.process = {
      exec: run,
      executeCommand: run,
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
    });
    return guardUnsupported(new Sandbox(box), "daytona");
  }

  async get(sandboxId: string): Promise<Sandbox> {
    const box = await this.#sdk.sandbox.getById(sandboxId);
    if (!box) throw new Error(`Sandbox ${sandboxId} not found`);
    return guardUnsupported(new Sandbox(box), "daytona");
  }

  async delete(sandbox: string | Sandbox): Promise<void> {
    const target = typeof sandbox === "string" ? await this.get(sandbox) : sandbox;
    await target.delete();
  }
}
