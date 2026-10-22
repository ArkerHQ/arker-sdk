import type { RunCommandOptions } from "computesdk";
import { randomUUID } from "node:crypto";
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
const E2B_HOME = "/home/user";

function identityCommand(command: string, options?: { user?: string; cwd?: string; env?: Record<string, string> }): string {
  const user = options?.user ?? "user";
  if (user !== "user" && user !== "root") {
    throw new ArkerError("unsupported_operation", "Arker E2B commands support only the default user or root", 422);
  }
  const assignments = { HOME: user === "user" ? E2B_HOME : "/root", USER: user, LOGNAME: user, ...options?.env };
  const exports = Object.entries(assignments).map(([key, value]) => {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new TypeError(`Invalid environment variable name: ${key}`);
    return `export ${key}=${shellQuote(value)};`;
  }).join(" ");
  const shell = `cd ${shellQuote(options?.cwd ?? E2B_HOME)} && ${exports} exec /bin/bash -c ${shellQuote(command)}`;
  return `runuser -u ${user} -- sh -c ${shellQuote(shell)}`;
}

function filesystemCommand(command: string, user: unknown): string {
  return user === undefined ? command : identityCommand(command, { user: user as string, cwd: "/" });
}

async function validateE2BIdentity(box: UniversalSandbox): Promise<void> {
  const result = await box.runCommand(
    "test \"$(getent passwd user)\" = 'user:x:1000:1000::/home/user:/bin/bash' "
      + "&& test \"$(getent group user)\" = 'user:x:1000:' "
      + "&& test \"$(stat -c '%u:%g:%a' /home/user)\" = '1000:1000:755'",
  );
  if (result.exitCode !== 0) {
    throw new ArkerError(
      "unsupported_operation",
      "Arker E2B compatibility requires an explicitly configured source VM with user:user at UID/GID 1000 and /home/user owned 1000:1000 mode 0755",
      422,
    );
  }
}

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
        const { env, envs, timeout, timeoutMs, cwd, user, ...rest } = options ?? {};
        if (env !== undefined && envs !== undefined) throw new TypeError("Pass envs, not both env and envs");
        if (timeout !== undefined && timeoutMs !== undefined) throw new TypeError("Pass timeoutMs, not both timeout and timeoutMs");
        if (this.#native?.commands) {
          try {
            return await this.#native.commands.run(command, {
              ...rest,
              ...(cwd !== undefined ? { cwd } : {}),
              ...(user !== undefined ? { user } : {}),
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
        assertSupportedObjectKeys(options, ["cwd", "env", "envs", "timeout", "timeoutMs", "background", "stdin", "onStdout", "onStderr", "user"], "e2b", "commands.run");
        const limit = timeoutMs ?? timeout;
        if (limit !== undefined && (!Number.isFinite(limit) || limit < 0)) throw new RangeError("timeoutMs must be nonnegative and finite");
        if (options?.background || options?.stdin || options?.onStdout || options?.onStderr) {
          const current = await this.#processes!.exec({
            command: `exec ${identityCommand(command, { user, cwd, env: envs ?? env })}`,
            workingDir: "/", env: {}, stdin: options?.stdin ?? false,
          });
          const handle = e2bCommandHandle(this.#processes!, current, {
            timeoutMs: limit, onStdout: options?.onStdout, onStderr: options?.onStderr,
          });
          return options?.background ? handle : handle.wait();
        }
        const result = await this.#box.runCommand(identityCommand(command, { user, cwd, env: envs ?? env }), {
          ...rest,
          ...(timeoutMs !== undefined || timeout !== undefined ? { timeout: timeoutMs ?? timeout } : {}),
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
        assertSupportedObjectKeys(options, ["format", "user"], "e2b", "files.read");
        const format = options?.format ?? "text";
        if (format !== "text" && format !== "bytes") {
          throw new ArkerError("unsupported_operation", "Arker files.read supports buffered text/bytes formats, not this format", 422);
        }
        const resolved = resolveCompatPath(path, E2B_HOME);
        if (options?.user !== undefined) {
          const staged = `/tmp/.computesdk-e2b-read-${randomUUID()}`;
          const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
          let readError: Error | undefined;
          let content: Uint8Array | undefined;
          try {
            const copied = await this.#box.runCommand(identityCommand(
              `cat -- ${shellQuote(resolved)} > ${shellQuote(staged)}`,
              { user: options.user as string },
            ));
            if (copied.exitCode !== 0) throw new Error(copied.stderr || `read failed with exit ${copied.exitCode}`);
            content = Uint8Array.from(await vm.sync(staged));
          } catch (error) { readError = error instanceof Error ? error : new Error(String(error)); }
          const cleanup = await this.#box.runCommand(`rm -f -- ${shellQuote(staged)}`);
          if (readError) throw readError;
          if (cleanup.exitCode !== 0) throw new Error(cleanup.stderr || `temporary file cleanup failed with exit ${cleanup.exitCode}`);
          if (!content) throw new Error("Arker file read returned no data");
          return format === "bytes" ? content : new TextDecoder().decode(content);
        }
        if (format === "bytes") {
          const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
          return Uint8Array.from(await vm.sync(resolved));
        }
        return this.#box.filesystem.readFile(resolved);
      },
      write: async (path: string, content: string | ArrayBuffer, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, ["user"], "e2b", "files.write");
        const resolved = resolveCompatPath(path, "/home/user");
        if (typeof content !== "string" && !(content instanceof ArrayBuffer)) {
          throw new ArkerError("unsupported_operation", "Arker files.write supports string/ArrayBuffer data, not this input", 422);
        }
        const staged = `/tmp/.computesdk-e2b-write-${randomUUID()}`;
        const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
        const parent = resolved.slice(0, resolved.lastIndexOf("/")) || "/";
        const userCommand = `mkdir -p -- ${shellQuote(parent)} && cat -- ${shellQuote(staged)} > ${shellQuote(resolved)}`;
        let writeError: Error | undefined;
        try {
          await vm.sync(staged, typeof content === "string" ? new TextEncoder().encode(content) : new Uint8Array(content));
          const written = await this.#box.runCommand(identityCommand(userCommand, {
            user: options?.user as string | undefined, cwd: "/",
          }));
          if (written.exitCode !== 0) writeError = new Error(written.stderr || `write failed with exit ${written.exitCode}`);
        } catch (error) {
          writeError = error instanceof Error ? error : new Error(String(error));
        }
        let cleanupError: Error | undefined;
        try {
          const cleanup = await this.#box.runCommand(`rm -f -- ${shellQuote(staged)}`);
          if (cleanup.exitCode !== 0) cleanupError = new Error(cleanup.stderr || `temporary file cleanup failed with exit ${cleanup.exitCode}`);
        } catch (error) {
          cleanupError = error instanceof Error ? error : new Error(String(error));
        }
        if (writeError) throw writeError;
        if (cleanupError) throw cleanupError;
        return { name: resolved.split("/").at(-1)!, type: FileType.FILE, path: resolved };
      },
      writeFiles: async (
        files: Array<{ path: string; data: string | ArrayBuffer | Blob | ReadableStream }>,
        options?: Record<string, unknown>,
      ) => {
        assertSupportedObjectKeys(options, ["user"], "e2b", "files.writeFiles");
        const written = [];
        for (const file of files) written.push(await (this.files as any).write(file.path, file.data, options));
        return written;
      },
      makeDir: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, ["user"], "e2b", "files.makeDir");
        const result = await this.#box.runCommand(identityCommand(
          `mkdir -p -- ${shellQuote(resolveCompatPath(path, E2B_HOME))}`,
          { user: options?.user as string | undefined, cwd: "/" },
        ));
        if (result.exitCode !== 0) throw new Error(result.stderr || `mkdir failed with exit ${result.exitCode}`);
      },
      list: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, ["user"], "e2b", "files.list");
        const resolved = resolveCompatPath(path, E2B_HOME);
        const script = `cd ${shellQuote(resolved)} 2>/dev/null || exit 3; for e in * .*; do `
          + `[ "$e" = "." ] && continue; [ "$e" = ".." ] && continue; [ -e "$e" ] || continue; `
          + `if [ -d "$e" ]; then t=dir; else t=file; fi; printf '%s\\t%s\\n' "$t" "$e"; done`;
        const result = await this.#box.runCommand(filesystemCommand(script, options?.user));
        if (result.exitCode !== 0) throw new Error(result.stderr || `list failed with exit ${result.exitCode}`);
        const names = result.stdout.split("\n").filter((line) => line.includes("\t")).map((line) => line.split("\t", 2)[1]!);
        return Promise.all(names.map((name) => (this.files as any).getInfo(`${resolved.replace(/\/$/, "")}/${name}`, options)));
      },
      exists: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, ["user"], "e2b", "files.exists");
        if (options?.user !== undefined) {
          return (await this.#box.runCommand(filesystemCommand(
            `test -e ${shellQuote(resolveCompatPath(path, E2B_HOME))}`, options.user,
          ))).exitCode === 0;
        }
        return this.#box.filesystem.exists(resolveCompatPath(path, "/home/user"));
      },
      remove: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, ["user"], "e2b", "files.remove");
        if (options?.user !== undefined) {
          const result = await this.#box.runCommand(filesystemCommand(
            `rm -rf -- ${shellQuote(resolveCompatPath(path, E2B_HOME))}`, options.user,
          ));
          if (result.exitCode !== 0) throw new Error(result.stderr || `remove failed with exit ${result.exitCode}`);
          return;
        }
        await this.#box.filesystem.remove(this.provider === "arker" ? resolveCompatPath(path, "/home/user") : path);
      },
      getInfo: async (path: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, ["user"], "e2b", "files.getInfo");
        const resolved = resolveCompatPath(path, "/home/user");
        const result = await this.#box.runCommand(filesystemCommand(
          `stat --printf '%n\\037%s\\037%f\\037%A\\037%U\\037%G\\037%Y\\037%F' -- ${shellQuote(resolved)}`,
          options?.user,
        ));
        if (result.exitCode !== 0) throw new Error(result.stderr || `stat failed with exit ${result.exitCode}`);
        const values = result.stdout.split("\x1f");
        if (values.length !== 8) throw new Error("Incomplete file information response");
        const [name, size, mode, permissions, owner, group, modified, description] = values as string[];
        let type = description.includes("directory") ? FileType.DIR : FileType.FILE;
        let symlinkTarget: string | undefined;
        if (description.includes("symbolic link")) {
          type = FileType.SYMLINK;
          const target = await this.#box.runCommand(filesystemCommand(`readlink -- ${shellQuote(resolved)}`, options?.user));
          if (target.exitCode === 0) symlinkTarget = target.stdout.replace(/\n$/, "");
        }
        return {
          name: name.replace(/\/$/, "").split("/").at(-1)!,
          type,
          path: name,
          size: Number(size),
          mode: Number.parseInt(mode, 16),
          permissions,
          owner,
          group,
          modifiedTime: new Date(Number(modified) * 1000),
          ...(symlinkTarget === undefined ? {} : { symlinkTarget }),
        };
      },
      rename: async (oldPath: string, newPath: string, options?: Record<string, unknown>) => {
        assertSupportedObjectKeys(options, ["user"], "e2b", "files.rename");
        const oldResolved = resolveCompatPath(oldPath, "/home/user");
        const newResolved = resolveCompatPath(newPath, "/home/user");
        const parent = newResolved.slice(0, newResolved.lastIndexOf("/")) || "/";
        const result = await this.#box.runCommand(filesystemCommand(
          `mkdir -p -- ${shellQuote(parent)} && mv -- ${shellQuote(oldResolved)} ${shellQuote(newResolved)}`,
          options?.user,
        ));
        if (result.exitCode !== 0) throw new Error(result.stderr || `rename failed with exit ${result.exitCode}`);
        return (this.files as any).getInfo(newResolved, options);
      },
    } as unknown as VendorSandbox["files"]);
    this.git = this.#native?.git ?? new Git(gitCommands(this.commands));
  }

  private static async wrap(
    box: UniversalSandbox,
    options: { validateIdentity?: boolean; deleteOnValidationFailure?: boolean } = {},
  ): Promise<Sandbox> {
    const native = nativeInstance(box);
    if (!native && box.provider === "arker" && options.validateIdentity !== false) {
      try {
        await validateE2BIdentity(box);
      } catch (error) {
        if (options.deleteOnValidationFailure) {
          const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
          await vm.delete().catch(() => undefined);
        }
        throw error;
      }
    }
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
    const timeout = resolveTimeout(opts) ?? 300_000;
    const box = await sdk((opts as any)?.routerConfigPath).sandbox.create({
      ...(templateId ? { templateId } : {}),
      timeout,
      ...((opts as any)?.envs ? { envs: (opts as any).envs } : {}),
      ...((opts as any)?.metadata ? { metadata: (opts as any).metadata } : {}),
      ...((opts as any)?.allowInternetAccess !== undefined ? { allowInternetAccess: (opts as any).allowInternetAccess } : {}),
    });
    return Sandbox.wrap(box, { deleteOnValidationFailure: true });
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
    return (await Sandbox.wrap(owner.instance, { validateIdentity: false })).kill();
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
