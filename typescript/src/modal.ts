import { modal as createModalProvider } from "@computesdk/modal";
import { ModalClient as NativeModalClient } from "modal";
import type { RunCommandOptions } from "computesdk";

import {
  argvToCommand,
  assertSupportedObjectKeys,
  guardUnsupported,
  isStringRecord,
  nativeInstance,
  type UniversalSandbox,
} from "./compat/common.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";

export interface ModalCompatConfig {
  /** Modal token ID used only for native Modal fallback. */
  tokenId?: string;
  /** Modal token secret used only for native Modal fallback. */
  tokenSecret?: string;
  /** Arker provider options. Falls back to ARKER_* environment variables. */
  arker?: ArkerComputeProviderConfig;
  routerConfigPath?: string;
}

interface ModalExecOptions {
  workdir?: string;
  env?: Record<string, string>;
  timeoutMs?: number;
  stdout?: "pipe";
  stderr?: "pipe";
  mode?: "text";
}

const MODAL_CONFIG_KEYS = ["tokenId", "tokenSecret", "arker", "routerConfigPath"] as const;
const MODAL_EXEC_OPTION_KEYS = ["workdir", "env", "timeoutMs", "stdout", "stderr", "mode"] as const;
const MODAL_CREATE_OPTION_KEYS = ["blockNetwork", "outboundCidrAllowlist", "outboundDomainAllowlist"] as const;

type ModalProvider = ReturnType<typeof createModalProvider>;

function lazyModalProvider(config?: Pick<ModalCompatConfig, "tokenId" | "tokenSecret">): Pick<ModalProvider, "name" | "sandbox"> {
  let provider: ModalProvider | null = null;
  const getProvider = (): ModalProvider => {
    provider ??= createModalProvider({
      tokenId: config?.tokenId,
      tokenSecret: config?.tokenSecret,
    });
    return provider;
  };

  return {
    name: "modal",
    sandbox: {
      create: (options) => getProvider().sandbox.create(options),
      getById: (sandboxId) => getProvider().sandbox.getById(sandboxId),
      destroy: (sandboxId) => getProvider().sandbox.destroy(sandboxId),
      list: () => getProvider().sandbox.list(),
    },
  };
}

function assertOptionValue(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

function toRunCommandOptions(opts: unknown): RunCommandOptions | undefined {
  assertSupportedObjectKeys(opts, MODAL_EXEC_OPTION_KEYS, "modal", "Sandbox.exec");
  if (opts === undefined) return undefined;
  const modalOpts = opts as ModalExecOptions;

  assertOptionValue(
    modalOpts.workdir === undefined || typeof modalOpts.workdir === "string",
    "Sandbox.exec option 'workdir' must be a string in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.timeoutMs === undefined || typeof modalOpts.timeoutMs === "number",
    "Sandbox.exec option 'timeoutMs' must be a number in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.env === undefined || isStringRecord(modalOpts.env),
    "Sandbox.exec option 'env' must be an object in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.stdout === undefined || modalOpts.stdout === "pipe",
    "Sandbox.exec only supports stdout: 'pipe' in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.stderr === undefined || modalOpts.stderr === "pipe",
    "Sandbox.exec only supports stderr: 'pipe' in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.mode === undefined || modalOpts.mode === "text",
    "Sandbox.exec only supports mode: 'text' in the Arker compatibility layer for modal.",
  );

  const runOptions: RunCommandOptions = {};
  if (modalOpts.workdir !== undefined) runOptions.cwd = modalOpts.workdir;
  if (modalOpts.env !== undefined) runOptions.env = modalOpts.env;
  if (modalOpts.timeoutMs !== undefined) runOptions.timeout = modalOpts.timeoutMs;

  return Object.keys(runOptions).length > 0 ? runOptions : undefined;
}

class ModalProcess {
  readonly stdout: { readText: () => Promise<string> };
  readonly stderr: { readText: () => Promise<string> };
  #exitCode: number;

  constructor(out: string, err: string, exitCode: number) {
    this.#exitCode = exitCode;
    this.stdout = { readText: async () => out };
    this.stderr = { readText: async () => err };
  }

  async wait(): Promise<number> {
    return this.#exitCode;
  }
}

export class Sandbox {
  readonly sandboxId: string;
  readonly provider: string;
  #box: UniversalSandbox;

  constructor(box: UniversalSandbox) {
    this.#box = box;
    this.sandboxId = box.sandboxId;
    this.provider = box.provider;
  }

  async exec(cmd: string[] | string, opts?: ModalExecOptions): Promise<ModalProcess> {
    const command = argvToCommand(cmd);
    const runOptions = toRunCommandOptions(opts) ?? {};
    if (this.provider === "arker" && runOptions.cwd === undefined) runOptions.cwd = "/";
    const result = Object.keys(runOptions).length > 0 ? await this.#box.runCommand(command, runOptions) : await this.#box.runCommand(command);
    return new ModalProcess(result.stdout, result.stderr, result.exitCode);
  }

  async terminate(): Promise<void> {
    await this.#box.destroy();
  }
}

export class ModalClient {
  #sdk: CompatSdk;
  #native: NativeModalClient | undefined;
  #nativeConfig: Pick<ModalCompatConfig, "tokenId" | "tokenSecret">;

  #nativeClient(): NativeModalClient {
    return this.#native ??= new NativeModalClient(this.#nativeConfig);
  }

  get apps(): NativeModalClient["apps"] { return this.#nativeClient().apps; }
  get images(): NativeModalClient["images"] { return this.#nativeClient().images; }

  close(): void { this.#native?.close(); }

  constructor(config?: ModalCompatConfig) {
    assertSupportedObjectKeys(config, MODAL_CONFIG_KEYS, "modal", "new ModalClient");
    this.#nativeConfig = { tokenId: config?.tokenId, tokenSecret: config?.tokenSecret };
    this.#sdk = createCompatSdk({
      arker: config?.arker,
      fallbackProvider: lazyModalProvider({
        tokenId: config?.tokenId,
        tokenSecret: config?.tokenSecret,
      }),
      routerConfigPath: config?.routerConfigPath,
    });
    return guardUnsupported(this, "modal");
  }

  readonly sandboxes = {
    create: async (...args: unknown[]): Promise<Sandbox> => {
      if (args.length > 3) throw new Error("ModalClient.sandboxes.create accepts at most app, image, and options");
      const options = (args.length >= 3 ? args[2] : args.length === 1 ? args[0] : undefined) as Record<string, unknown> | undefined;
      if (args.length === 1 && options && "appId" in options) {
        throw new Error("ModalClient.sandboxes.create requires both app and image");
      }
      assertSupportedObjectKeys(options, MODAL_CREATE_OPTION_KEYS, "modal", "ModalClient.sandboxes.create");
      const handles = args.length >= 2 ? { modalApp: args[0], modalImage: args[1] } : {};
      if (args.length >= 2 && (!args[0] || typeof args[0] !== "object" || !args[1] || typeof args[1] !== "object")) {
        throw new Error("ModalClient.sandboxes.create requires native app and image handles");
      }
      const box = await this.#sdk.sandbox.create({
        ...handles,
        ...(typeof options?.blockNetwork === "boolean" ? { blockNetwork: options.blockNetwork } : {}),
        ...(Array.isArray(options?.outboundCidrAllowlist) ? { outboundCidrAllowlist: options.outboundCidrAllowlist } : {}),
        ...(Array.isArray(options?.outboundDomainAllowlist) ? { outboundDomainAllowlist: options.outboundDomainAllowlist } : {}),
      });
      return guardUnsupported(new Sandbox(box), "modal", nativeInstance(box));
    },
    fromId: async (sandboxId: string): Promise<Sandbox> => {
      const box = await this.#sdk.sandbox.getById(sandboxId);
      if (!box) throw new Error(`Sandbox ${sandboxId} not found`);
      return guardUnsupported(new Sandbox(box), "modal", nativeInstance(box));
    },
  };
}
export * from "modal";
