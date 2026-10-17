import { e2b as createE2BProvider } from "@computesdk/e2b";
import type { RunCommandOptions } from "computesdk";
import { CommandExitError, type CommandStartOpts } from "e2b";

export { CommandExitError } from "e2b";

import {
  assertSupportedObjectKeys,
  guardUnsupported,
  nativeInstance,
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
  error?: string;
}

type CommandOptions = RunCommandOptions & CommandStartOpts;
interface NativeSandbox {
  getHost?: (port: number) => string;
  commands?: { run: (command: string, options?: CommandStartOpts) => Promise<any> };
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
  #native?: NativeSandbox;
  #arkerHostname?: string;

  readonly commands: {
    run: (command: string, options?: CommandOptions) => Promise<CommandExitResult>;
  };

  readonly files: {
    read: (path: string) => Promise<string>;
    write: (path: string, content: string) => Promise<void>;
    makeDir: (path: string) => Promise<void>;
    list: (path: string) => Promise<unknown[]>;
    exists: (path: string) => Promise<boolean>;
    remove: (path: string) => Promise<void>;
  };

  private constructor(box: UniversalSandbox, native?: object, arkerHostname?: string) {
    this.#box = box;
    this.#native = native as NativeSandbox | undefined;
    this.#arkerHostname = arkerHostname;
    this.sandboxId = box.sandboxId;
    this.provider = box.provider;
    this.commands = {
      run: async (command, options) => {
        const { env, envs, timeout, timeoutMs, ...rest } = options ?? {};
        if (env !== undefined && envs !== undefined) throw new TypeError("Pass envs, not both env and envs");
        if (timeout !== undefined && timeoutMs !== undefined) throw new TypeError("Pass timeoutMs, not both timeout and timeoutMs");
        if (this.#native?.commands) {
          try {
            return await this.#native.commands.run(command, {
              ...rest,
              ...(envs !== undefined || env !== undefined ? { envs: envs ?? env } : {}),
              ...(timeoutMs !== undefined || timeout !== undefined ? { timeoutMs: timeoutMs ?? timeout } : {}),
            });
          } catch (error) {
            // A provider can resolve another installed copy of the same vendor SDK.
            const exit = error as Partial<CommandExitError> | null;
            if (!(error instanceof CommandExitError) && exit?.name === "CommandExitError"
              && Number.isInteger(exit.exitCode) && exit.exitCode !== 0
              && typeof exit.stdout === "string" && typeof exit.stderr === "string"
              && (exit.error === undefined || typeof exit.error === "string")) {
              throw new CommandExitError({ stdout: exit.stdout, stderr: exit.stderr, exitCode: exit.exitCode!, error: exit.error });
            }
            throw error;
          }
        }
        assertSupportedObjectKeys(options, ["cwd", "env", "envs", "timeout", "timeoutMs", "background"], "e2b", "commands.run");
        if (options?.background) throw new TypeError("E2B background CommandHandle is not yet supported on Arker");
        const result = await this.#box.runCommand(command, {
          ...rest,
          ...(envs !== undefined || env !== undefined ? { env: envs ?? env } : {}),
          ...(timeoutMs !== undefined || timeout !== undefined ? { timeout: timeoutMs ?? timeout } : {}),
          ...(this.provider === "arker" && options?.cwd === undefined ? { cwd: "/home/user" } : {}),
        });
        const output = {
          stdout: result.stdout,
          stderr: result.stderr,
          exitCode: result.exitCode,
          error: result.exitCode === 0 ? undefined : `exit status ${result.exitCode}`,
        };
        if (result.exitCode !== 0) throw new CommandExitError(output);
        return output;
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

  private static async wrap(box: UniversalSandbox): Promise<Sandbox> {
    const native = nativeInstance(box);
    const arkerHostname = box.provider === "arker"
      ? await box.getUrl({ port: 8080 }).then((url) => new URL(url).hostname).catch(() => undefined)
      : undefined;
    return guardUnsupported(new Sandbox(box, native, arkerHostname), "e2b", native);
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
    return Sandbox.wrap(box);
  }

  static async connect(sandboxId: string, opts?: Pick<CreateOptions, "routerConfigPath">): Promise<Sandbox> {
    assertSupportedObjectKeys(opts, ["routerConfigPath"], "e2b", "Sandbox.connect");
    const box = await sdk(opts?.routerConfigPath).sandbox.getById(sandboxId);
    if (!box) throw new Error(`Sandbox ${sandboxId} not found`);
    return Sandbox.wrap(box);
  }

  getHost(port: number): string {
    if (this.#native?.getHost) return this.#native.getHost(port);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error("port must be an integer from 1 through 65535");
    if (!this.#arkerHostname) throw new Error("Arker did not return an inbound hostname for this sandbox");
    return port === 8080 ? this.#arkerHostname : this.#arkerHostname.replace(/^([^.]+)/, `$1-${port}`);
  }

  async kill(): Promise<void> {
    await this.#box.destroy();
  }
}
export * from "e2b";
