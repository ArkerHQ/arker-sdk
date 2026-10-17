import { e2b as createE2BProvider } from "@computesdk/e2b";
import type { RunCommandOptions } from "computesdk";
import { ArkerError, type VM } from "./index.js";
import { CommandExitError, Sandbox as VendorSandbox, NotFoundError, type CommandStartOpts, type CommandHandle,
  type Commands as VendorCommands, type CommandConnectOpts, type CommandRequestOpts } from "e2b";

export { CommandExitError } from "e2b";
export type { CommandHandle } from "e2b";

import {
  assertSupportedObjectKeys,
  guardUnsupported,
  nativeInstance,
  resolveTimeout,
  type CreateOptions,
  type UniversalSandbox,
  resolveCompatPath,
  shellQuote,
} from "./compat/common.js";
import { createCompatSdk, createCompatRouter } from "./compat/runtime.js";
import { GuestProcesses, GuestProcessError } from "./compat/guest-process.js";
import { e2bCommandHandle, e2bProcessError } from "./compat/e2b-command.js";

export interface CommandExitResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  error?: string;
}

type CommandOptions = RunCommandOptions & CommandStartOpts;
interface CommandRunner {
  (command: string, options: CommandOptions & { background: true }): Promise<CommandHandle>;
  (command: string, options?: CommandOptions & { background?: false }): Promise<CommandExitResult>;
  (command: string, options?: CommandOptions): Promise<CommandExitResult | CommandHandle>;
}
interface NativeSandbox {
  kill: () => Promise<boolean>;
  getHost?: (port: number) => string;
  commands?: VendorCommands;
}

const E2B_CREATE_OPTION_KEYS = ["timeout", "timeoutMs", "routerConfigPath", "envs", "metadata", "allowInternetAccess"] as const;

function routingOptions(routerConfigPath?: string) {
  const fallbackProvider = Object.assign(
    createE2BProvider({ nativeSdk: { Sandbox: VendorSandbox, NotFoundError, CommandExitError } }),
    {
      ownershipProbe: async (id: string): Promise<boolean> => {
        try {
          await VendorSandbox.getInfo(id);
          return true;
        } catch (error) {
          if (error instanceof NotFoundError) return false;
          throw error;
        }
      },
    },
  );
  return {
    fallbackProvider,
    routerConfigPath,
  };
}

function sdk(routerConfigPath?: string) {
  return createCompatSdk(routingOptions(routerConfigPath));
}

export class Sandbox {
  readonly sandboxId: string;
  readonly provider: string;
  #box: UniversalSandbox;
  #native?: NativeSandbox;
  #arkerHostname?: string;
  #processes?: GuestProcesses;

  readonly commands: {
    run: CommandRunner;
    connect: (pid: number, options?: CommandConnectOpts) => Promise<CommandHandle>;
    kill: (pid: number, options?: CommandRequestOpts) => Promise<boolean>;
    sendStdin: (pid: number, data: string | Uint8Array, options?: CommandRequestOpts) => Promise<void>;
    closeStdin: (pid: number, options?: CommandRequestOpts) => Promise<void>;
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
    this.#processes = box.provider === "arker" ? new GuestProcesses(box) : undefined;
    this.commands = {
      run: (async (command: string, options?: CommandOptions): Promise<CommandExitResult | CommandHandle> => {
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
        assertSupportedObjectKeys(options, ["cwd", "env", "envs", "timeout", "timeoutMs", "background", "stdin", "onStdout", "onStderr"], "e2b", "commands.run");
        const limit = timeoutMs ?? timeout;
        if (limit !== undefined && (!Number.isFinite(limit) || limit < 0)) throw new RangeError("timeoutMs must be nonnegative and finite");
        if (options?.background || options?.stdin || options?.onStdout || options?.onStderr) {
          const current = await this.#processes!.exec({
            command: `exec /bin/bash -lc ${shellQuote(command)}`, workingDir: options?.cwd ?? "/home/user",
            env: envs ?? env ?? {}, stdin: options?.stdin ?? false,
          });
          const handle = e2bCommandHandle(this.#processes!, current, {
            timeoutMs: limit, onStdout: options?.onStdout, onStderr: options?.onStderr,
          });
          return options?.background ? handle : handle.wait();
        }
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
      }) as CommandRunner,
      connect: async (pid, options) => {
        if (this.#native?.commands) return this.#native.commands.connect(pid, options);
        assertSupportedObjectKeys(options, ["timeoutMs", "onStdout", "onStderr"], "e2b", "commands.connect");
        const current = await this.#processes!.get(String(pid)).catch((error) => { throw e2bProcessError(error); });
        return e2bCommandHandle(this.#processes!, current, options);
      },
      kill: async (pid, options) => {
        if (this.#native?.commands) return this.#native.commands.kill(pid, options);
        assertSupportedObjectKeys(options, [], "e2b", "commands.kill");
        try {
          if ((await this.#processes!.get(String(pid))).status !== "running") return false;
        } catch (error) {
          if (error instanceof GuestProcessError && error.code === "process_not_found") return false;
          throw error;
        }
        await this.#processes!.kill(String(pid));
        return true;
      },
      sendStdin: async (pid, data, options) => {
        if (this.#native?.commands) return this.#native.commands.sendStdin(pid, data, options);
        assertSupportedObjectKeys(options, [], "e2b", "commands.sendStdin");
        await this.#processes!.writeStdin(String(pid), data).catch((error) => { throw e2bProcessError(error); });
      },
      closeStdin: async (pid, options) => {
        if (this.#native?.commands) return this.#native.commands.closeStdin(pid, options);
        assertSupportedObjectKeys(options, [], "e2b", "commands.closeStdin");
        await this.#processes!.closeStdin(String(pid)).catch((error) => { throw e2bProcessError(error); });
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

  static async kill(sandboxId: string, opts?: Pick<CreateOptions, "routerConfigPath">): Promise<boolean> {
    assertSupportedObjectKeys(opts, ["routerConfigPath"], "e2b", "Sandbox.kill");
    const owner = await createCompatRouter(routingOptions(opts?.routerConfigPath)).getOwner(sandboxId);
    if (!owner) return false;
    if (owner.provider === "e2b") return VendorSandbox.kill(sandboxId);
    return (await Sandbox.wrap(owner.instance)).kill();
  }

  getHost(port: number): string {
    if (this.#native?.getHost) return this.#native.getHost(port);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error("port must be an integer from 1 through 65535");
    if (!this.#arkerHostname) throw new Error("Arker did not return an inbound hostname for this sandbox");
    return port === 8080 ? this.#arkerHostname : this.#arkerHostname.replace(/^([^.]+)/, `$1-${port}`);
  }

  async kill(): Promise<boolean> {
    if (this.#native) return this.#native.kill();
    const vm = (this.#box as UniversalSandbox & { getInstance(): VM }).getInstance();
    try {
      await vm.refresh();
      return (await vm.delete()).deleted;
    } catch (error) {
      if (error instanceof ArkerError && error.status === 404) return false;
      throw error;
    }
  }
}
export * from "e2b";
