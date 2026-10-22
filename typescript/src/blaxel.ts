import { SandboxInstance as NativeBlaxelSandbox, settings as blaxelSettings } from "@blaxel/core";
import { chmod, readFile, writeFile } from "node:fs/promises";
import { posix } from "node:path";
import type { VM } from "./index.js";

import { assertSupportedObjectKeys, CompatibilityNotFoundError, guardUnsupported, resolveCompatPath, shellQuote, type UniversalSandbox } from "./compat/common.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";
import { GuestProcesses } from "./compat/guest-process.js";
import { privateFetch } from "./compat/private-fetch.js";
import { createNativeBlaxelProvider } from "./compat/blaxel-provider.js";
import { nativeInstance } from "./compat/common.js";

export interface BlaxelCompatConfig {
  apiKey?: string;
  workspace?: string;
  arker?: ArkerComputeProviderConfig;
  routerConfigPath?: string;
}

export interface BlaxelCreateOptions {
  name?: string;
  image?: string;
  memory?: number;
  ttl?: string | number;
  envs?: Record<string, string>;
  runtime?: string;
  region?: string;
  ports?: Array<{ target: number; protocol?: string }>;
  labels?: Record<string, string>;
  network?: NonNullable<NativeBlaxelSandbox["spec"]>["network"];
  safe?: boolean;
  timeout?: number;
}

export interface BlaxelCreateCallOptions extends BlaxelCompatConfig {
  safe?: boolean;
  createIfNotExist?: boolean;
  timeout?: number;
}

type BlaxelProcessRequest = Omit<Parameters<NativeBlaxelSandbox["process"]["exec"]>[0], "command"> & {
  command: string | string[];
  working_dir?: string;
  envs?: Record<string, string>;
};

type BlaxelCompatProcess = Omit<NativeBlaxelSandbox["process"], "exec"> & {
  exec(request: string | BlaxelProcessRequest): Promise<any>;
};

type BlaxelCompatFileSystem = Omit<NativeBlaxelSandbox["fs"], "write" | "mkdir" | "rm"> & {
  write(path: string, content: string | Uint8Array): Promise<any>;
  mkdir(path: string, permissions?: string): Promise<any>;
  rm(path: string, recursive?: boolean): Promise<any>;
};

function filesystemDisplayPath(path: string): string {
  return resolveCompatPath(path, "/blaxel").replace(/\/+/g, "/");
}

function filesystemPath(path: string): string {
  return posix.resolve(filesystemDisplayPath(path));
}

function filesystemFilePath(path: string): string {
  if (path.startsWith("/") || !path.includes("/")) return filesystemPath(path);
  return posix.resolve("/", path);
}

function parseTtl(ttl: string | number | undefined): number | undefined {
  if (ttl === undefined || typeof ttl === "number") return ttl;
  const match = /^(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)$/i.exec(ttl.trim());
  if (!match) throw new Error(`Invalid Blaxel ttl '${ttl}'; use milliseconds or a duration such as 30s, 5m, 2h, or 1d`);
  const multiplier = { ms: 1, s: 1_000, m: 60_000, h: 3_600_000, d: 86_400_000 }[match[2]!.toLowerCase()]!;
  return Number(match[1]) * multiplier;
}

function makeSdk(config: BlaxelCompatConfig = {}): CompatSdk {
  return createCompatSdk({
    arker: config.arker,
    fallbackProvider: createNativeBlaxelProvider({
      apiKey: config.apiKey,
      workspace: config.workspace,
    }),
    routerConfigPath: config.routerConfigPath,
  });
}

interface FetchProfile {
  region?: string;
  disableH2: boolean;
}

function fetchProfile(region?: string): FetchProfile {
  return { region: region || blaxelSettings.region, disableH2: blaxelSettings.disableH2 };
}

export class SandboxInstance {
  readonly id: string;
  readonly provider: string;
  readonly metadata: NativeBlaxelSandbox["metadata"];
  readonly network: NativeBlaxelSandbox["network"];
  declare readonly previews: NativeBlaxelSandbox["previews"];
  declare readonly schedules: NativeBlaxelSandbox["schedules"];
  declare readonly sessions: NativeBlaxelSandbox["sessions"];
  declare readonly codegen: NativeBlaxelSandbox["codegen"];
  declare readonly system: NativeBlaxelSandbox["system"];
  declare readonly drives: NativeBlaxelSandbox["drives"];
  declare readonly snapshots: NativeBlaxelSandbox["snapshots"];
  declare readonly status: NativeBlaxelSandbox["status"];
  declare readonly events: NativeBlaxelSandbox["events"];
  declare readonly spec: NativeBlaxelSandbox["spec"];
  declare readonly lastUsedAt: NativeBlaxelSandbox["lastUsedAt"];
  declare readonly errors: NativeBlaxelSandbox["errors"];
  declare readonly h2Domain: NativeBlaxelSandbox["h2Domain"];
  declare readonly expiresIn: NativeBlaxelSandbox["expiresIn"];
  declare readonly snapshot: NativeBlaxelSandbox["snapshot"];
  declare readonly listSnapshots: NativeBlaxelSandbox["listSnapshots"];
  declare readonly deleteSnapshot: NativeBlaxelSandbox["deleteSnapshot"];
  declare readonly restore: NativeBlaxelSandbox["restore"];
  declare readonly fork: NativeBlaxelSandbox["fork"];
  declare readonly wait: NativeBlaxelSandbox["wait"];
  declare readonly archive: NativeBlaxelSandbox["archive"];
  declare readonly unarchive: NativeBlaxelSandbox["unarchive"];
  #box: UniversalSandbox;
  #native?: NativeBlaxelSandbox;
  #h2Eligible: boolean;

  readonly process: BlaxelCompatProcess;
  readonly fs: BlaxelCompatFileSystem;

  private constructor(box: UniversalSandbox, native?: NativeBlaxelSandbox, profile = fetchProfile()) {
    this.#box = box;
    this.#native = native;
    this.#h2Eligible = Boolean(profile.region) && !profile.disableH2;
    const network = guardUnsupported({ fetch: this.fetch.bind(this) }, "blaxel");
    this.network = native?.network ?? network as unknown as NativeBlaxelSandbox["network"];
    this.id = box.sandboxId;
    this.provider = box.provider;
    const vm = box.provider === "arker" ? (box as UniversalSandbox & { getInstance(): VM }).getInstance() : undefined;
    this.metadata = (native?.metadata ?? { name: vm?.name ?? box.sandboxId }) as NativeBlaxelSandbox["metadata"];
    const guest = new GuestProcesses(box);
    this.process = (native?.process ?? {
      exec: async (request) => {
        const value = typeof request === "string" ? { command: request } : request;
        assertSupportedObjectKeys(value, ["command", "workingDir", "working_dir", "env", "envs", "timeout", "waitForCompletion", "name", "stdin", "onStdout", "onStderr", "onLog"], "blaxel", "process.exec");
        const command = Array.isArray(value.command)
          ? value.command.map((part) => `'${part.replaceAll("'", "'\\''")}'`).join(" ")
          : value.command;
        const { working_dir, envs, ...options } = value;
        return guest.exec({
          ...options,
          command,
          signalExitCode: -1,
          ...(value.timeout !== undefined ? { timeout: value.timeout * 1000 } : {}),
          workingDir: value.workingDir ?? working_dir,
          env: value.env ?? envs,
          waitForCompletion: typeof request === "string" ? true : value.waitForCompletion,
        });
      },
      get: guest.get.bind(guest),
      list: guest.list.bind(guest),
      wait: guest.wait.bind(guest),
      kill: guest.kill.bind(guest),
      stop: guest.stop.bind(guest),
      logs: guest.logs.bind(guest),
      writeStdin: guest.writeStdin.bind(guest),
      closeStdin: guest.closeStdin.bind(guest),
    }) as BlaxelCompatProcess;
    this.fs = (native?.fs ?? {
      read: (path) => box.filesystem.readFile(this.provider === "arker" ? filesystemFilePath(path) : path),
      write: (path, content) => {
        if (typeof content !== "string") throw new TypeError("fs.write requires text; use fs.writeBinary for binary content");
        const resolved = filesystemFilePath(path);
        return box.filesystem.writeFile(resolved, content).then(() => ({ message: "File created/updated successfully", path: filesystemDisplayPath(path) }));
      },
      writeBinary: async (path, content) => {
        const bytes = typeof content === "string" ? await readFile(content)
          : content instanceof Uint8Array ? content
          : new Uint8Array(await content.arrayBuffer());
        const resolved = filesystemFilePath(path);
        const vm = (box as unknown as { getInstance(): VM }).getInstance();
        await vm.sync(resolved, bytes);
        return { message: "Binary file uploaded successfully", path: filesystemDisplayPath(path) };
      },
      readBinary: async (path) => {
        const vm = (box as unknown as { getInstance(): VM }).getInstance();
        const bytes = await vm.sync(filesystemFilePath(path));
        return new Blob([new Uint8Array(bytes)]);
      },
      download: async (src, destinationPath, { mode = 0o644 } = {}) => {
        const vm = (box as unknown as { getInstance(): VM }).getInstance();
        const bytes = await vm.sync(filesystemFilePath(src));
        await writeFile(destinationPath, bytes);
        await chmod(destinationPath, mode);
      },
      mkdir: async (path, permissions = "0755") => {
        if (!/^0?[0-7]{3}$/.test(permissions)) {
          throw new Error("fs.mkdir permissions must be a three- or four-digit octal mode");
        }
        const resolved = filesystemPath(path);
        const quoted = shellQuote(resolved);
        const result = await box.runCommand(`mkdir -p -- ${quoted} && chmod -- ${permissions} ${quoted}`);
        if (result.exitCode !== 0) throw new Error(result.stderr || `mkdir failed with exit ${result.exitCode}`);
        return { message: "Directory created successfully", path: filesystemDisplayPath(path) };
      },
      ls: async (path) => {
        const display = filesystemDisplayPath(path);
        const entries = await box.filesystem.readdir(filesystemPath(path));
        const withPath = (entry: { name: string }) => ({
          ...entry,
          path: posix.resolve(posix.join(display, entry.name)),
        });
        return {
          name: posix.basename(display.replace(/\/+$/, "")) || "/",
          path: display,
          files: entries.filter((entry) => entry.type === "file").map(withPath),
          subdirectories: entries.filter((entry) => entry.type === "directory").map(withPath),
        };
      },
      rm: async (path, recursive = false) => {
        const resolved = filesystemPath(path);
        const quoted = shellQuote(resolved);
        const directoryCommand = recursive ? "rm -r" : "rmdir";
        const result = await box.runCommand(
          `if [ -d ${quoted} ] && [ ! -L ${quoted} ]; then ` +
          `${directoryCommand} -- ${quoted} && printf directory; else rm -- ${quoted} && printf file; fi`,
        );
        if (result.exitCode !== 0) throw new Error(result.stderr || `remove failed with exit ${result.exitCode}`);
        return { message: `${result.stdout === "directory" ? "Directory" : "File"} deleted successfully`, path: filesystemDisplayPath(path) };
      },
      cp: async (source, destination, { maxWait = 180_000 } = {}) => {
        if (!Number.isFinite(maxWait) || maxWait <= 0) throw new RangeError("maxWait must be positive and finite");
        const result = await box.runCommand(
          `cp -r -- ${shellQuote(filesystemFilePath(source))} ${shellQuote(filesystemFilePath(destination))}`,
          { timeout: Math.ceil(maxWait / 1000) * 1000 },
        );
        if (result.exitCode !== 0) throw new Error(result.stderr || `Could not copy ${source} to ${destination}`);
        return { message: "Files copied", source, destination };
      },
      writeTree: async (files, destinationPath = null) => {
        const destination = destinationPath ?? "";
        for (const file of files) {
          if (typeof file.path !== "string" || typeof file.content !== "string") {
            throw new TypeError("writeTree files require string path and content");
          }
          await box.filesystem.writeFile(filesystemFilePath(posix.join(destination, file.path)), file.content);
        }
        return (this.fs as BlaxelCompatFileSystem).ls(destination || "/blaxel");
      },
      find: async (path, options = {}) => {
        const { type, patterns, maxResults, excludeDirs, excludeHidden } = options;
        if (type !== undefined && type !== "file" && type !== "directory") {
          throw new TypeError("find type must be 'file' or 'directory'");
        }
        if (maxResults !== undefined && (!Number.isInteger(maxResults) || maxResults < 0)) {
          throw new RangeError("maxResults must be a nonnegative integer");
        }
        const predicates: string[] = [];
        if (type) predicates.push("-type", type === "file" ? "f" : "d");
        if (patterns?.length) predicates.push(`\\( ${patterns.map((pattern) => `-name ${shellQuote(pattern)}`).join(" -o ")} \\)`);
        for (const directory of excludeDirs ?? []) predicates.push(`! -path ${shellQuote(`*/${directory}/*`)}`);
        if (excludeHidden) predicates.push("! -path '*/.*'");
        const result = await box.runCommand(
          `find ${shellQuote(filesystemPath(path))} -mindepth 1 ${predicates.join(" ")} -printf '%y\\0%p\\0'`,
        );
        if (result.exitCode !== 0) throw new Error(result.stderr || `find failed with exit ${result.exitCode}`);
        const fields = result.stdout.split("\0");
        if (fields.at(-1) !== "" || (fields.length - 1) % 2 !== 0) throw new Error("Incomplete find response");
        const matches = [];
        for (let index = 0; index < fields.length - 1; index += 2) {
          matches.push({ path: fields[index + 1]!, type: fields[index] === "d" ? "directory" : "file" });
        }
        const limited = maxResults === undefined ? matches : matches.slice(0, maxResults);
        return { matches: limited, total: limited.length };
      },
      grep: async (query, path = "/", options = {}) => {
        const { caseSensitive, contextLines, maxResults, filePattern, excludeDirs } = options;
        if (contextLines !== undefined && contextLines !== 0) {
          throw new Error("Arker fs.grep contextLines are not supported");
        }
        const flags = ["-R", "-n", "--null", "-F"];
        if (caseSensitive === false) flags.push("-i");
        if (filePattern) flags.push("--include", filePattern);
        for (const directory of excludeDirs ?? []) flags.push("--exclude-dir", directory);
        const result = await box.runCommand(
          ["grep", ...flags.map(shellQuote), "--", shellQuote(query), shellQuote(filesystemPath(path))].join(" "),
        );
        if (result.exitCode !== 0 && result.exitCode !== 1) {
          throw new Error(result.stderr || `grep failed with exit ${result.exitCode}`);
        }
        const matches = result.stdout.split("\n").filter(Boolean).map((record) => {
          const separator = record.indexOf("\0");
          const colon = record.indexOf(":", separator + 1);
          const line = Number(record.slice(separator + 1, colon));
          if (separator < 0 || colon < 0 || !Number.isInteger(line)) throw new Error("Incomplete grep response");
          const text = record.slice(colon + 1);
          const needle = caseSensitive === false ? query.toLowerCase() : query;
          const haystack = caseSensitive === false ? text.toLowerCase() : text;
          return { path: record.slice(0, separator), line, column: Math.max(haystack.indexOf(needle), 0) + 1, text };
        });
        const limited = maxResults === undefined ? matches : matches.slice(0, maxResults);
        return { matches: limited, query, total: limited.length };
      },
      watch: () => {
        throw new Error("Arker fs.watch requires an ordered filesystem event stream and is not supported");
      },
      search: async () => {
        throw new Error("Arker fs.search cannot reproduce Blaxel's server-side fuzzy scoring and ordering");
      },
    }) as BlaxelCompatFileSystem;
    return guardUnsupported(this, "blaxel", native);
  }

  async fetch(port: number, path = "/", init?: RequestInit): Promise<Response> {
    if (this.#native) return this.#native.fetch(port, path, init);
    const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
    return privateFetch(vm, port, path, init, this.#h2Eligible && !blaxelSettings.disableH2 ? "blaxel-h2" : "fetch");
  }

  private static async wrap(box: UniversalSandbox, profile?: FetchProfile): Promise<SandboxInstance> {
    if (box.provider !== "blaxel") return new SandboxInstance(box, undefined, profile);
    return new SandboxInstance(box, nativeInstance(box) as NativeBlaxelSandbox);
  }

  static async create(options: BlaxelCreateOptions = {}, callOptions: BlaxelCreateCallOptions = {}): Promise<SandboxInstance> {
    const profile = fetchProfile(options.region);
    const box = await makeSdk(callOptions).sandbox.create({
      name: options.name,
      image: options.image,
      memory: options.memory,
      timeout: parseTtl(options.ttl),
      envs: options.envs,
      runtime: options.runtime,
      ...(options.region !== undefined ? { region: options.region } : {}),
      ...(options.ports !== undefined ? { ports: options.ports } : {}),
      ...(options.labels !== undefined ? { labels: options.labels } : {}),
      ...(options.network !== undefined ? { network: options.network } : {}),
      ...((callOptions.safe ?? options.safe) !== undefined ? { safe: callOptions.safe ?? options.safe } : {}),
      createIfNotExist: callOptions.createIfNotExist ?? false,
      ...((callOptions.timeout ?? options.timeout) !== undefined
        ? { creationTimeout: callOptions.timeout ?? options.timeout }
        : {}),
    });
    if (box.provider === "arker") {
      try { await box.filesystem.mkdir("/blaxel"); }
      catch (error) {
        await box.destroy();
        throw error;
      }
    }
    return SandboxInstance.wrap(box, profile);
  }

  static async createIfNotExists(
    options: BlaxelCreateOptions = {},
    callOptions: Omit<BlaxelCreateCallOptions, "createIfNotExist"> = {},
  ): Promise<SandboxInstance> {
    if (!options.name) throw new Error("createIfNotExists requires a Blaxel sandbox name");
    try {
      return await SandboxInstance.get(options.name, callOptions);
    } catch (error) {
      if (!(error instanceof CompatibilityNotFoundError)) throw error;
      return SandboxInstance.create(options, { ...callOptions, createIfNotExist: true });
    }
  }

  static async get(id: string, config: BlaxelCompatConfig = {}): Promise<SandboxInstance> {
    const box = await makeSdk(config).sandbox.getById(id);
    if (!box) throw new CompatibilityNotFoundError(`Sandbox ${id} not found`);
    return SandboxInstance.wrap(box);
  }

  static async delete(id: string, config: BlaxelCompatConfig = {}): Promise<void> {
    await makeSdk(config).sandbox.destroy(id);
  }

  async delete(): Promise<void> {
    await this.#box.destroy();
  }
}

export class Blaxel {
  constructor(private readonly config: BlaxelCompatConfig = {}) {}
  create(options: BlaxelCreateOptions = {}) { return SandboxInstance.create(options, this.config); }
  get(id: string) { return SandboxInstance.get(id, this.config); }
  delete(id: string) { return SandboxInstance.delete(id, this.config); }
}
export * from "@blaxel/core";
