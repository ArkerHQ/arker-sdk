import { DaytonaError } from "@daytona/sdk";
import { Daytona as NativeDaytona, type DaytonaConfig as NativeDaytonaConfig, type Sandbox as NativeSandbox } from "@daytona/sdk";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { createNativeDaytonaProvider } from "./compat/daytona-provider.js";
import { DaytonaFileSystem, daytonaGuestCommand } from "./compat/daytona-filesystem.js";
import { createDaytonaSessions, type SessionAPI } from "./compat/daytona-sessions.js";
import { createDaytonaTerminals, type TerminalAPI } from "./compat/daytona-pty.js";
import { DaytonaVolumeService } from "./compat/daytona-volumes.js";
import * as lifecycle from "./compat/daytona-lifecycle.js";
import { ArkerComputeSandbox } from "./compat/arker-provider.js";
import { ArkerError } from "./index.js";

import { assertSupportedObjectKeys, guardUnsupported, nativeInstance, type UniversalSandbox } from "./compat/common.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";

export interface DaytonaCompatConfig extends NativeDaytonaConfig {
  /** Daytona API key used only for native Daytona fallback. */
  apiKey?: string;
  /** Arker provider options. Falls back to ARKER_* environment variables. */
  arker?: ArkerComputeProviderConfig;
  routerConfigPath?: string;
}

type NativeExecuteResponse = Awaited<ReturnType<NativeSandbox["process"]["executeCommand"]>>;

export interface ExecuteResponse extends NativeExecuteResponse {
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

const DAYTONA_CONFIG_KEYS = ["apiKey", "jwtToken", "organizationId", "apiUrl", "serverUrl", "target", "otelEnabled", "useDeprecatedPolling", "requestTimeoutMs", "_experimental", "arker", "routerConfigPath"] as const;
const DAYTONA_CREATE_OPTION_KEYS = ["image", "snapshot", "envVars", "name", "language", "resources", "volumes", "networkBlockAll", "networkAllowList", "domainAllowList"] as const;

type DaytonaEnvReader = { get(name: string): string | undefined };
type DaytonaEnvReaderConstructor = new () => DaytonaEnvReader;

let daytonaEnvReaderConstructor: DaytonaEnvReaderConstructor | undefined;

function loadDaytonaEnvReader(): DaytonaEnvReaderConstructor {
  if (daytonaEnvReaderConstructor) return daytonaEnvReaderConstructor;
  const require = createRequire(typeof import.meta.url === "string" ? import.meta.url : __filename);
  const entry = require.resolve("@daytona/sdk");
  const runtime = require(join(dirname(entry), "utils", "Runtime.js")) as {
    DaytonaEnvReader?: DaytonaEnvReaderConstructor;
  };
  if (!runtime.DaytonaEnvReader) throw new Error("Installed @daytona/sdk does not expose its credential environment reader");
  return (daytonaEnvReaderConstructor = runtime.DaytonaEnvReader);
}

function hasNativeDaytonaCredentials(config: NativeDaytonaConfig): boolean {
  const reader = new (loadDaytonaEnvReader())();
  let apiKey = !config.apiKey && config.jwtToken ? undefined : config.apiKey;
  let jwtToken = config.jwtToken;
  apiKey ||= jwtToken ? undefined : reader.get("DAYTONA_API_KEY");
  jwtToken ||= reader.get("DAYTONA_JWT_TOKEN");
  return !!(apiKey || jwtToken);
}

function arkerCreateOptions(options: Record<string, unknown>): Record<string, unknown> {
  if (options.image !== undefined && typeof options.image !== "string") {
    throw new ArkerError("unsupported_operation", "Daytona Image builders are not supported on Arker; use a published image reference or route to Daytona", 422);
  }
  const { resources, volumes: _volumes, ...rest } = options;
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
  readonly fs: Pick<NativeSandbox["fs"], "createFolder" | "deleteFile" | "getFileDetails" | "listFiles" | "moveFiles" | "setFilePermissions" | "uploadFile" | "uploadFiles" | "downloadFile" | "downloadFiles" | "uploadFileStream" | "downloadFileStream">;
  #box: UniversalSandbox;

  readonly process: TerminalAPI & SessionAPI & {
    exec: (command: string, options?: ExecuteCommandOptions) => Promise<ExecuteResponse>;
    executeCommand: (
      command: string,
      cwd?: string,
      env?: Record<string, string>,
      timeout?: number,
    ) => Promise<NativeExecuteResponse>;
  };

  constructor(box: UniversalSandbox, private readonly lifecycleConfig?: ArkerComputeProviderConfig) {
    this.#box = box;
    this.id = box.sandboxId;
    this.provider = box.provider;
    const nativeFilesystem = (nativeInstance(box) as NativeSandbox | undefined)?.fs;
    this.fs = nativeFilesystem ?? new DaytonaFileSystem(box);
    const run = async (command: string, options?: ExecuteCommandOptions): Promise<ExecuteResponse> => {
      if (options?.env) {
        for (const key of Object.keys(options.env)) {
          if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new TypeError("Invalid environment variable name");
        }
      }
      const { cwd, env, ...transport } = options ?? {};
      const result = await this.#box.runCommand(
        this.provider === "arker" ? daytonaGuestCommand({ operation: "exec", command, cwd, env }) : command,
        {
          ...transport,
          ...(this.provider !== "arker" ? { cwd, env } : {}),
          ...(options?.timeout !== undefined ? { timeout: options.timeout * 1_000 } : {}),
        },
      );
      return {
        exitCode: result.exitCode,
        result: result.stdout,
        stdout: result.stdout,
        stderr: result.stderr,
      };
    };
    const native = nativeInstance(box) as { process?: { executeCommand: (command: string, cwd?: string, env?: Record<string, string>, timeout?: number) => Promise<NativeExecuteResponse> } } | undefined;
    const nativeProcess = (nativeInstance(box) as NativeSandbox | undefined)?.process;
    const terminals: TerminalAPI = nativeProcess ? {
      createPty: (...args) => nativeProcess.createPty(...args),
      connectPty: (...args) => nativeProcess.connectPty(...args),
      listPtySessions: (...args) => nativeProcess.listPtySessions(...args),
      getPtySessionInfo: (...args) => nativeProcess.getPtySessionInfo(...args),
      resizePtySession: (...args) => nativeProcess.resizePtySession(...args),
      killPtySession: (...args) => nativeProcess.killPtySession(...args),
    } : createDaytonaTerminals(box);
    const sessions: SessionAPI = nativeProcess ? {
      createSession: (...args) => nativeProcess.createSession(...args),
      getSession: (...args) => nativeProcess.getSession(...args),
      listSessions: (...args) => nativeProcess.listSessions(...args),
      deleteSession: (...args) => nativeProcess.deleteSession(...args),
      getSessionCommand: (...args) => nativeProcess.getSessionCommand(...args),
      executeSessionCommand: (...args) => nativeProcess.executeSessionCommand(...args),
      getSessionCommandLogs: ((...args: Parameters<SessionAPI["getSessionCommandLogs"]>) => nativeProcess.getSessionCommandLogs(...args)) as SessionAPI["getSessionCommandLogs"],
      sendSessionCommandInput: (...args) => nativeProcess.sendSessionCommandInput(...args),
    } : createDaytonaSessions(box);
    this.process = {
      ...terminals,
      ...sessions,
      exec: run,
      executeCommand: typeof native?.process?.executeCommand === "function"
        ? native.process.executeCommand.bind(native.process)
        : async (command, cwd, env, timeout) => {
          const result = await run(command, { cwd, env, timeout });
          return { exitCode: result.exitCode, result: result.result, artifacts: { stdout: result.result } };
        },
    };
  }

  async getUserHomeDir(): Promise<string | undefined> {
    const native = nativeInstance(this.#box) as NativeSandbox | undefined;
    if (native) return native.getUserHomeDir();
    const result = await this.process.executeCommand('printf "%s" "$HOME"');
    if (result.exitCode !== 0) throw new DaytonaError("Failed to get user home directory");
    return result.result;
  }

  async pause(timeout = 60): Promise<void> {
    const native = nativeInstance(this.#box) as NativeSandbox | undefined;
    if (native) return native.pause(timeout);
    return lifecycle.pause((this.#box as ArkerComputeSandbox).getInstance(), timeout);
  }

  async start(timeout = 60): Promise<void> {
    const native = nativeInstance(this.#box) as NativeSandbox | undefined;
    if (native) return native.start(timeout);
    return lifecycle.start((this.#box as ArkerComputeSandbox).getInstance(), timeout);
  }

  async stop(timeout = 60, force = false): Promise<void> {
    const native = nativeInstance(this.#box) as NativeSandbox | undefined;
    if (native) return native.stop(timeout, force);
    return lifecycle.unsupported("stop");
  }

  async archive(): Promise<void> {
    const native = nativeInstance(this.#box) as NativeSandbox | undefined;
    if (native) return native.archive();
    return lifecycle.unsupported("archive");
  }

  async fork(params?: { name?: string }, timeout = 60): Promise<Sandbox | NativeSandbox> {
    const native = nativeInstance(this.#box) as NativeSandbox | undefined;
    if (native) return native.fork(params, timeout);
    const child = await lifecycle.fork((this.#box as ArkerComputeSandbox).getInstance(), params, timeout, this.lifecycleConfig);
    return new Sandbox(new ArkerComputeSandbox(child, async () => { await child.delete(); }), this.lifecycleConfig);
  }

  async delete(): Promise<void> {
    await this.#box.destroy();
  }
}

export class Daytona {
  #sdk: CompatSdk;
  #lifecycleConfig?: ArkerComputeProviderConfig;
  #closeNative: () => Promise<void>;
  #native?: NativeDaytona;
  readonly volume: DaytonaVolumeService;

  constructor(config?: DaytonaCompatConfig) {
    assertSupportedObjectKeys(config, DAYTONA_CONFIG_KEYS, "daytona", "new Daytona");
    const { arker, routerConfigPath, ...native } = config ?? {};
    this.#lifecycleConfig = arker;
    const hasArker = !!(arker?.apiKey || globalThis.process?.env?.ARKER_API_KEY?.trim());
    const nativeEnabled = hasNativeDaytonaCredentials(native);
    if (!hasArker && !nativeEnabled) this.#native = new NativeDaytona(native);
    const nativeClient = () => (this.#native ??= new NativeDaytona(native));
    this.volume = new DaytonaVolumeService(arker, () => nativeClient().volume, routerConfigPath, nativeEnabled);
    const provider = createNativeDaytonaProvider(native, nativeClient, async (options) => {
      await this.volume.assertNativeMounts((options as Record<string, unknown> | undefined)?.volumes);
    });
    this.#closeNative = provider.close;
    this.#sdk = createCompatSdk({
      arker,
      fallbackProvider: provider,
      routerConfigPath,
      mapArkerCreateOptions: arkerCreateOptions,
      prepareArkerCreateOptions: async (options) => ({
        options: arkerCreateOptions(options),
        context: await this.volume.resolveMounts(options.volumes),
      }),
      finishArkerCreate: (sandbox, mounts) => this.volume.attach(sandbox, mounts as Array<{ filesystemId: string; mountPath: string; subpath?: string }>),
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
      ...(source.volumes !== undefined ? { volumes: source.volumes } : {}),
      ...(typeof source.networkBlockAll === "boolean" ? { networkBlockAll: source.networkBlockAll } : {}),
      ...(typeof source.networkAllowList === "string" ? { networkAllowList: source.networkAllowList } : {}),
      ...(typeof source.domainAllowList === "string" ? { domainAllowList: source.domainAllowList } : {}),
    };
    // ComputeSDK currently narrows `resources` to Vercel's shape; the native
    // Daytona provider forwards its own SDK's resources without that narrowing.
    const box = await this.#sdk.sandbox.create(request as Parameters<CompatSdk["sandbox"]["create"]>[0]);
    if (box.provider === "arker") await box.filesystem.mkdir("/home/daytona");
    return guardUnsupported(new Sandbox(box, this.#lifecycleConfig), "daytona", nativeInstance(box));
  }

  async get(sandboxId: string): Promise<Sandbox> {
    const box = await this.#sdk.sandbox.getById(sandboxId);
    if (!box) throw new Error(`Sandbox ${sandboxId} not found`);
    return guardUnsupported(new Sandbox(box, this.#lifecycleConfig), "daytona", nativeInstance(box));
  }

  async start(sandbox: Sandbox | NativeSandbox, timeout?: number): Promise<void> {
    return sandbox.start(timeout);
  }

  async stop(sandbox: Sandbox | NativeSandbox): Promise<void> {
    return sandbox.stop();
  }

  async fork(sandbox: Sandbox | NativeSandbox, params?: { name?: string }, timeout = 60): Promise<Sandbox | NativeSandbox> {
    return sandbox.fork(params, timeout);
  }

  async delete(sandbox: string | Sandbox): Promise<void> {
    const target = typeof sandbox === "string" ? await this.get(sandbox) : sandbox;
    await target.delete();
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.#closeNative();
    await this.#native?.[Symbol.asyncDispose]();
  }
}
export * from "@daytona/sdk";
