import { e2b as createE2BProvider } from "@computesdk/e2b";
import type { RunCommandOptions } from "computesdk";

import {
  assertSupportedObjectKeys,
  guardUnsupported,
  resolveTimeout,
  type CreateOptions,
  type UniversalSandbox,
  resolveCompatPath,
} from "./compat/common.js";
import { createCompatSdk } from "./compat/runtime.js";

export interface CommandExitResult {
  stdout: string;
  stderr: string;
  exitCode: number;
}

const E2B_CREATE_OPTION_KEYS = ["timeout", "timeoutMs", "routerConfigPath", "envs", "metadata", "allowInternetAccess"] as const;

function sdk(routerConfigPath?: string) {
  return createCompatSdk({
    fallbackProvider: createE2BProvider({}),
    routerConfigPath,
  });
}

export class Sandbox {
  readonly sandboxId: string;
  readonly provider: string;
  #box: UniversalSandbox;

  readonly commands: {
    run: (command: string, options?: RunCommandOptions) => Promise<CommandExitResult>;
  };

  readonly files: {
    read: (path: string) => Promise<string>;
    write: (path: string, content: string) => Promise<void>;
    makeDir: (path: string) => Promise<void>;
    list: (path: string) => Promise<unknown[]>;
    exists: (path: string) => Promise<boolean>;
    remove: (path: string) => Promise<void>;
  };

  private constructor(box: UniversalSandbox) {
    this.#box = box;
    this.sandboxId = box.sandboxId;
    this.provider = box.provider;
    this.commands = {
      run: async (command, options) => {
        const result = await this.#box.runCommand(command, {
          ...options,
          ...(this.provider === "arker" && options?.cwd === undefined ? { cwd: "/home/user" } : {}),
        });
        return {
          stdout: result.stdout,
          stderr: result.stderr,
          exitCode: result.exitCode,
        };
      },
    };
    this.files = {
      read: (path) => this.#box.filesystem.readFile(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path),
      write: async (path, content) => {
        await this.#box.filesystem.writeFile(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path, content);
      },
      makeDir: async (path) => {
        await this.#box.filesystem.mkdir(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path);
      },
      list: (path) => this.#box.filesystem.readdir(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path),
      exists: (path) => this.#box.filesystem.exists(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path),
      remove: async (path) => {
        await this.#box.filesystem.remove(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path);
      },
    };
  }

  static async create(opts?: CreateOptions): Promise<Sandbox>;
  static async create(templateId: string | undefined, opts?: CreateOptions): Promise<Sandbox>;
  static async create(templateOrOptions?: string | CreateOptions, options?: CreateOptions): Promise<Sandbox> {
    const templateId = typeof templateOrOptions === "string" ? templateOrOptions : undefined;
    const opts = typeof templateOrOptions === "string" ? options : templateOrOptions ?? options;
    assertSupportedObjectKeys(opts, E2B_CREATE_OPTION_KEYS, "e2b", "Sandbox.create");
    const timeout = resolveTimeout(opts);
    const box = await sdk((opts as any)?.routerConfigPath).sandbox.create({
      ...(templateId ? { templateId } : {}),
      ...(timeout !== undefined ? { timeout } : {}),
      ...((opts as any)?.envs ? { envs: (opts as any).envs } : {}),
      ...((opts as any)?.metadata ? { metadata: (opts as any).metadata } : {}),
      ...((opts as any)?.allowInternetAccess !== undefined ? { allowInternetAccess: (opts as any).allowInternetAccess } : {}),
    });
    return guardUnsupported(new Sandbox(box), "e2b");
  }

  static async connect(sandboxId: string, opts?: Pick<CreateOptions, "routerConfigPath">): Promise<Sandbox> {
    assertSupportedObjectKeys(opts, ["routerConfigPath"], "e2b", "Sandbox.connect");
    const box = await sdk(opts?.routerConfigPath).sandbox.getById(sandboxId);
    if (!box) throw new Error(`Sandbox ${sandboxId} not found`);
    return guardUnsupported(new Sandbox(box), "e2b");
  }

  async kill(): Promise<void> {
    await this.#box.destroy();
  }
}
