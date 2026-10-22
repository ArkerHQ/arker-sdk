import type { RunCommandOptions } from "computesdk";
import { ArkerError, type VM } from "./index.js";
import { CommandExitError, FileType, Git, Sandbox as VendorSandbox, type CommandStartOpts, type CommandHandle,
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
import { createNativeE2BProvider } from "./compat/e2b-provider.js";

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
  files?: VendorSandbox["files"];
  git?: VendorSandbox["git"];
}

function gitCommands(commands: Sandbox["commands"]): VendorCommands {
  return {
    run: (command: string, options?: CommandStartOpts) =>
      commands.run(command, {
        ...(options?.envs !== undefined ? { envs: options.envs } : {}),
        ...(options?.cwd !== undefined ? { cwd: options.cwd } : {}),
        ...(options?.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {}),
        ...(options?.user !== undefined ? { user: options.user } : {}),
        ...(options?.requestTimeoutMs !== undefined ? { requestTimeoutMs: options.requestTimeoutMs } : {}),
      }),
  } as VendorCommands;
}

const E2B_CREATE_OPTION_KEYS = ["timeout", "timeoutMs", "routerConfigPath", "envs", "metadata", "allowInternetAccess"] as const;

function routingOptions(routerConfigPath?: string) {
  return {
    fallbackProvider: createNativeE2BProvider(),
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

  readonly files: VendorSandbox["files"];
  readonly git: VendorSandbox["git"];

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
    this.files = this.#native?.files ?? ({
      read: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, ["format"], "e2b", "files.read");
        const format = options?.format ?? "text";
        if (format !== "text" && format !== "bytes") {
          throw new ArkerError("unsupported_operation", "Arker files.read supports buffered text/bytes formats, not this format", 422);
        }
        const resolved = resolveCompatPath(path, "/home/user");
        if (format === "bytes") {
          const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
          return Uint8Array.from(await vm.sync(resolved));
        }
        return this.#box.filesystem.readFile(resolved);
      },
      write: async (path: string, content: string | ArrayBuffer, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, [], "e2b", "files.write");
        const resolved = resolveCompatPath(path, "/home/user");
        if (typeof content === "string") {
          await this.#box.filesystem.writeFile(resolved, content);
        } else if (content instanceof ArrayBuffer) {
          const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
          await vm.sync(resolved, new Uint8Array(content));
        } else {
          throw new ArkerError("unsupported_operation", "Arker files.write supports string/ArrayBuffer data, not this input", 422);
        }
        return { name: resolved.split("/").at(-1)!, type: FileType.FILE, path: resolved };
      },
      writeFiles: async (
        files: Array<{ path: string; data: string | ArrayBuffer | Blob | ReadableStream }>,
        options?: Record<string, unknown>,
      ) => {
        assertSupportedObjectKeys(options, [], "e2b", "files.writeFiles");
        const written = [];
        for (const file of files) written.push(await (this.files as any).write(file.path, file.data));
        return written;
      },
      makeDir: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, [], "e2b", "files.makeDir");
        await this.#box.filesystem.mkdir(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path);
      },
      list: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, [], "e2b", "files.list");
        return this.#box.filesystem.readdir(resolveCompatPath(path, "/home/user"));
      },
      exists: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, [], "e2b", "files.exists");
        return this.#box.filesystem.exists(resolveCompatPath(path, "/home/user"));
      },
      remove: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, [], "e2b", "files.remove");
        await this.#box.filesystem.remove(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path);
      },
      getInfo: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, [], "e2b", "files.getInfo");
        const resolved = resolveCompatPath(path, "/home/user");
        const result = await this.#box.runCommand(
          `stat --printf '%n\\037%s\\037%f\\037%A\\037%U\\037%G\\037%Y\\037%F' -- ${shellQuote(resolved)}`,
        );
        if (result.exitCode !== 0) throw new Error(result.stderr || `stat failed with exit ${result.exitCode}`);
        const values = result.stdout.split("\x1f");
        if (values.length !== 8) throw new Error("Incomplete file information response");
        const [name, size, mode, permissions, owner, group, modified, description] = values as string[];
        let type = description.includes("directory") ? FileType.DIR : FileType.FILE;
        let symlinkTarget: string | undefined;
        if (description.includes("symbolic link")) {
          type = FileType.SYMLINK;
          const target = await this.#box.runCommand(`readlink -- ${shellQuote(resolved)}`);
          if (target.exitCode === 0) symlinkTarget = target.stdout.replace(/\n$/, "");
        }
        return {
          name: name.replace(/\/$/, "").split("/").at(-1)!,
          type,
          path: name,
          size: Number(size),
          mode: Number.parseInt(mode, 16),
          permissions: permissions.length === 10 ? permissions.slice(1) : permissions,
          owner,
          group,
          modifiedTime: new Date(Number(modified) * 1000),
          ...(symlinkTarget === undefined ? {} : { symlinkTarget }),
        };
      },
      rename: async (oldPath: string, newPath: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, [], "e2b", "files.rename");
        const oldResolved = resolveCompatPath(oldPath, "/home/user");
        const newResolved = resolveCompatPath(newPath, "/home/user");
        const parent = newResolved.slice(0, newResolved.lastIndexOf("/")) || "/";
        const result = await this.#box.runCommand(
          `mkdir -p -- ${shellQuote(parent)} && mv -- ${shellQuote(oldResolved)} ${shellQuote(newResolved)}`,
        );
        if (result.exitCode !== 0) throw new Error(result.stderr || `rename failed with exit ${result.exitCode}`);
        return (this.files as any).getInfo(newResolved);
      },
    } as unknown as VendorSandbox["files"]);
    this.git = this.#native?.git ?? new Git(gitCommands(this.commands));
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
