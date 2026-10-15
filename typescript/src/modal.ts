import { modal as createModalProvider } from "@computesdk/modal";
import type { RunCommandOptions } from "computesdk";

import {
  argvToCommand,
  assertNoUnsupportedArgs,
  assertSupportedObjectKeys,
  guardUnsupported,
  isStringRecord,
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
    const runOptions = toRunCommandOptions(opts);
    const result = runOptions ? await this.#box.runCommand(command, runOptions) : await this.#box.runCommand(command);
    return new ModalProcess(result.stdout, result.stderr, result.exitCode);
  }

  async terminate(): Promise<void> {
    await this.#box.destroy();
  }
}

export class ModalClient {
  #sdk: CompatSdk;

  constructor(config?: ModalCompatConfig) {
    assertSupportedObjectKeys(config, MODAL_CONFIG_KEYS, "modal", "new ModalClient");
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
      assertNoUnsupportedArgs(args, "modal", "ModalClient.sandboxes.create");
      const box = await this.#sdk.sandbox.create({});
      return guardUnsupported(new Sandbox(box), "modal");
    },
    fromId: async (sandboxId: string): Promise<Sandbox> => {
      const box = await this.#sdk.sandbox.getById(sandboxId);
      if (!box) throw new Error(`Sandbox ${sandboxId} not found`);
      return guardUnsupported(new Sandbox(box), "modal");
    },
  };
}
