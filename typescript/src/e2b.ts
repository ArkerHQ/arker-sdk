/**
 * E2B-compatible sandboxes backed by Arker VMs.
 *
 * Replace `from "e2b"` with `from "@arker-ai/sdk/e2b"`. What this layer cannot
 * honor throws rather than doing something else.
 */
import {
  CompatGit,
  CompatSandbox,
  Volumes,
  type GitFileChange,
  type GitOptions,
  type GitState,
  type Egress,
  guardUnsupported,
  permissions,
  splitAddresses,
  unsupported,
  importOptional,
  newPortAuth,
  rejectOptions,
  type Change,
  type CompatConfig,
  type CommandResult,
  type Entry,
  type Errors,
  type OutputCallback,
  type Process,
  type Terminal,
} from "./compat/core.js";
import type { VM } from "./index.js";

export type { CommandResult } from "./compat/core.js";

// E2B runs commands, and resolves relative paths, from this directory.
const HOME = "/home/user";
const DEFAULT_SANDBOX_TIMEOUT_MS = 300_000;
const DEFAULT_COMMAND_TIMEOUT_MS = 60_000;
// Requests to the ports of a sandbox created with allowPublicTraffic: false carry its token in this header.
const TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token";
// E2B's own page size when listing.
const DEFAULT_PAGE_SIZE = 100;
const WATCH_INTERVAL_MS = 500;

// Options E2B uses to reach its own service; they have no meaning on Arker.
const E2B_CONNECTION_OPTIONS = new Set([
  "accessToken",
  "apiKey",
  "debug",
  "domain",
  "headers",
  "idempotencyKey",
  "logger",
  "requestTimeoutMs",
  "sandboxUrl",
  "secure",
  "signal",
]);

export class SandboxError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = new.target.name;
  }
}
export class TimeoutError extends SandboxError {}
export class InvalidArgumentError extends SandboxError {}
export class NotFoundError extends SandboxError {}
export class TemplateError extends NotFoundError {}
export class SandboxNotFoundError extends NotFoundError {}
export class FileNotFoundError extends NotFoundError {}
export class AuthenticationError extends SandboxError {}
export class RateLimitError extends SandboxError {}
export class ServiceBusyError extends SandboxError {}
export class GuestUnresponsiveError extends SandboxError {}
export class SandboxNotRunningError extends TimeoutError {}

export class NotSupportedError extends InvalidArgumentError {}

export class CommandExitError extends SandboxError implements CommandResult {
  constructor(private readonly result: CommandResult) {
    super(`Command exited with code ${result.exitCode} and error:\n${result.stderr}`);
  }
  get exitCode(): number {
    return this.result.exitCode;
  }
  get error(): string | undefined {
    return this.result.error;
  }
  get stdout(): string {
    return this.result.stdout;
  }
  get stderr(): string {
    return this.result.stderr;
  }
}

/** @internal */
export const ERRORS: Errors = {
  provider: "E2B",
  unsupported: NotSupportedError,
  invalid: InvalidArgumentError,
  notFound: TemplateError,
  fileNotFound: FileNotFoundError,
  timeout: TimeoutError,
  failed: SandboxError,
  auth: AuthenticationError,
  rateLimit: RateLimitError,
};

export enum FileType {
  FILE = "file",
  DIR = "dir",
}

export interface WriteInfo {
  name: string;
  type?: FileType;
  path: string;
}

export interface EntryInfo extends WriteInfo {
  type: FileType;
  size: number;
  mode: number;
  permissions: string;
  owner: string;
  group: string;
  modifiedTime?: Date;
  symlinkTarget?: string;
}

export enum FilesystemEventType {
  CHMOD = "chmod",
  CREATE = "create",
  REMOVE = "remove",
  RENAME = "rename",
  WRITE = "write",
}

export interface FilesystemEvent {
  name: string;
  type: FilesystemEventType;
  entry?: EntryInfo;
}

export interface ProcessInfo {
  pid: number;
  tag?: string;
  cmd: string;
  args: string[];
  envs: Record<string, string>;
  cwd?: string;
}

/** Options every file call takes; commands and files are root's on Arker. */
export interface FilesystemRequestOpts {
  user?: string;
  requestTimeoutMs?: number;
  signal?: AbortSignal;
  [option: string]: unknown;
}

export interface WatchOpts extends FilesystemRequestOpts {
  /** Stop watching after this long; 0 never stops. Defaults to 60 000. */
  timeoutMs?: number;
  onExit?: (err?: Error) => void | Promise<void>;
  recursive?: boolean;
}

export interface PtyCreateOpts {
  cols: number;
  rows: number;
  onData: (data: Uint8Array) => void | Promise<void>;
  /** Accepted, not enforced: a terminal runs until it exits or is killed. */
  timeoutMs?: number;
  user?: string;
  envs?: Record<string, string>;
  cwd?: string;
  [option: string]: unknown;
}

export interface SandboxInfo {
  sandboxId: string;
  templateId: string | undefined;
  name?: string;
  metadata: Record<string, string>;
  startedAt: Date;
  state: string | undefined;
}

export interface CommandStartOpts {
  background?: boolean;
  cwd?: string;
  /** Accepted for compatibility; commands run as root. */
  user?: string;
  envs?: Record<string, string>;
  /** Without `background`, called once with the full output when the command finishes. */
  onStdout?: OutputCallback;
  onStderr?: OutputCallback;
  stdin?: boolean;
  shell?: readonly string[];
  timeoutMs?: number;
  [option: string]: unknown;
}

export type UploadData = string | ArrayBuffer | Uint8Array | Blob | ReadableStream | AsyncIterable<Uint8Array>;
export type WriteEntry = { path: string; data: UploadData };

export interface SandboxOpts {
  template?: string;
  timeoutMs?: number;
  metadata?: Record<string, string>;
  envs?: Record<string, string>;
  cpuCount?: number;
  allowInternetAccess?: boolean;
  network?: SandboxNetworkOpts;
  lifecycle?: { onTimeout: "pause" | "kill"; autoResume?: boolean };
  /** Mount paths, and the volumes (or volume names) to mount there. */
  volumeMounts?: Record<string, Volume | string>;
  /** Where and how to reach Arker, and which VM to start from. */
  arker?: CompatConfig;
  [option: string]: unknown;
}

export const ALL_TRAFFIC = "0.0.0.0/0";

export interface SandboxNetworkSelectorContext {
  allTraffic: string;
  rules: Record<string, unknown>;
}

export type SandboxNetworkSelector = string[] | ((ctx: SandboxNetworkSelectorContext) => string[]);

export interface SandboxNetworkOpts {
  allowOut?: SandboxNetworkSelector;
  denyOut?: SandboxNetworkSelector;
  dnsServers?: string[];
  allowPublicTraffic?: boolean;
  [option: string]: unknown;
}

function selector(value: SandboxNetworkSelector | undefined): string[] {
  return typeof value === "function" ? value({ allTraffic: ALL_TRAFFIC, rules: {} }) : (value ?? []);
}

/** E2B's network options as Arker outbound rules: allow beats deny, and domains are allowed only with all other traffic denied. */
function egressFor(network: SandboxNetworkOpts = {}, allowInternetAccess?: boolean): Egress | undefined {
  const { allowOut, denyOut, allowPublicTraffic: _allowPublicTraffic, ...rest } = network;
  // Arker answers a sandbox's DNS queries itself, for the names its rules allow,
  // whichever nameserver they are sent to.
  rejectOptions(ERRORS, "network", rest, new Set(), new Set(["dnsServers"]));
  const [allowIps, allowHosts] = splitAddresses(selector(allowOut));
  const [denyIps, denyHosts] = splitAddresses(selector(denyOut));
  if (denyHosts.length > 0) throw new InvalidArgumentError(`denyOut takes IPs and CIDR blocks, not domains: ${denyHosts.join(", ")}`);
  if (allowHosts.includes("*")) throw unsupported(ERRORS, "network allowOut ['*']", "list the domains to allow.");
  const denyAll = allowInternetAccess === false || denyIps.includes(ALL_TRAFFIC);
  if (allowHosts.length > 0 && !denyAll) throw new InvalidArgumentError("Allowing domains in allowOut needs denyOut: [ALL_TRAFFIC].");
  const deny = denyIps.filter((ip) => ip !== ALL_TRAFFIC);
  if (allowIps.length + allowHosts.length + deny.length === 0 && !denyAll) return undefined;
  return { allowIps, allowHosts, denyIps: deny, denyAll };
}

export interface SandboxPauseOpts extends SandboxApiOpts {
  keepMemory?: boolean;
  mode?: "full" | "filesystem";
  strict?: boolean;
}

export interface SandboxForkOpts extends SandboxApiOpts {
  timeoutMs?: number;
  count?: number;
}

export interface CreateSnapshotOpts extends SandboxApiOpts {
  name?: string;
  mode?: "full" | "filesystem";
}

export interface SandboxConnectOpts {
  timeoutMs?: number;
  /** Arker always resumes a sandbox with its memory: only `"restore"` is supported. */
  onResume?: "restore" | "reboot";
  arker?: CompatConfig;
  [option: string]: unknown;
}

/** Options for calls that act on one sandbox by id. */
export interface SandboxApiOpts {
  arker?: CompatConfig;
  [option: string]: unknown;
}

export interface SandboxListOpts extends SandboxApiOpts {
  query?: {
    metadata?: Record<string, string>;
    /** Arker VMs resume when used, so every sandbox counts as running. */
    state?: Array<"running" | "paused">;
    startedAfter?: Date;
    template?: string;
  };
  order?: "asc" | "desc";
  limit?: number;
  nextToken?: string;
}

export interface SnapshotListOpts extends SandboxApiOpts {
  sandboxId?: string;
  name?: string;
  limit?: number;
  nextToken?: string;
}

export interface SnapshotInfo {
  snapshotId: string;
  names: string[];
}

/** Pages of `limit` items; call `nextItems()` while `hasNext`. */
class Paginator<T> {
  #items: AsyncIterator<T>;
  #limit: number;
  #cursor: ((item: T) => string) | undefined;
  #buffer: T[] = [];
  hasNext = true;
  nextToken: string | undefined = undefined;

  constructor(items: AsyncIterable<T>, limit: number | undefined, cursor?: (item: T) => string) {
    this.#items = items[Symbol.asyncIterator]();
    this.#limit = limit || DEFAULT_PAGE_SIZE;
    this.#cursor = cursor;
  }

  async nextItems(_opts?: SandboxApiOpts): Promise<T[]> {
    if (!this.hasNext) throw new SandboxError("No more items to fetch");
    // One item past the page tells whether another page follows.
    const page = this.#buffer;
    while (page.length <= this.#limit) {
      const next = await this.#items.next();
      if (next.done) break;
      page.push(next.value);
    }
    this.#buffer = page.splice(this.#limit);
    this.hasNext = this.#buffer.length > 0;
    this.nextToken = this.hasNext && this.#cursor ? this.#cursor(page[page.length - 1]!) : undefined;
    return page;
  }
}

export class SandboxPaginator extends Paginator<SandboxInfo> {}
export class SnapshotPaginator extends Paginator<SnapshotInfo> {}

function sandboxInfo(vm: VM, metadata: Record<string, string>): SandboxInfo {
  return {
    sandboxId: vm.id,
    templateId: vm.root_source_vm_name ?? undefined,
    name: vm.name ?? undefined,
    metadata,
    startedAt: new Date(vm.created_at ?? Date.now()),
    state: vm.state ?? undefined,
  };
}

function snapshotInfo(snapshot: CompatSandbox): SnapshotInfo {
  return { snapshotId: snapshot.vm.name!, names: [snapshot.vm.name!] };
}

/** Creation order: created_at is in whole seconds, and the ULID that ends a VM id breaks ties. */
function listKey(info: SandboxInfo): string {
  return `${info.startedAt.toISOString()} ${info.sandboxId.split("-").pop()}`;
}

function listToken(info: SandboxInfo): string {
  return Buffer.from(listKey(info)).toString("base64url");
}

function afterToken(token: string): string {
  const key = Buffer.from(token, "base64url").toString();
  if (!/^\S+ \S+$/.test(key)) throw new InvalidArgumentError(`Invalid nextToken '${token}'`);
  return key;
}

function matches(sandbox: CompatSandbox, query: NonNullable<SandboxListOpts["query"]>): boolean {
  const vm = sandbox.vm;
  return (
    Object.entries(query.metadata ?? {}).every(([key, value]) => sandbox.settings[key] === value) &&
    (!query.state?.length || query.state.includes("running")) &&
    (!query.startedAfter || new Date(vm.created_at!) >= query.startedAfter) &&
    (query.template === undefined || query.template === vm.root_source_vm_name)
  );
}

async function* fromArray<T>(items: T[]): AsyncGenerator<T> {
  yield* items;
}

function absolute(path: string): string {
  return path.startsWith("/") ? path : `${HOME}/${path}`;
}

function basename(path: string): string {
  return path.replace(/\/+$/, "").split("/").pop() ?? path;
}

function entryInfo(entry: Entry): EntryInfo {
  return {
    name: entry.name,
    type: entry.isDir ? FileType.DIR : FileType.FILE,
    path: entry.path,
    size: entry.size,
    mode: entry.mode,
    permissions: permissions(entry.mode),
    owner: entry.owner,
    group: entry.group,
    modifiedTime: new Date(entry.modified * 1_000),
    ...(entry.symlinkTarget ? { symlinkTarget: entry.symlinkTarget } : {}),
  };
}

// Options that only shape how E2B reaches its service or moves file data.
const REQUEST_OPTIONS = new Set(["gzip", "requestTimeoutMs", "signal", "useOctetStream"]);

/** Commands and files are root's on Arker, so another `user` warns that it is ignored. */
function checkOptions(method: string, options: Record<string, unknown> = {}): void {
  const { user, ...rest } = options;
  rejectOptions(ERRORS, method, { ...rest, user: user === "root" ? undefined : user }, REQUEST_OPTIONS, new Set(["user"]));
}

function checked(result: CommandResult): CommandResult {
  if (result.exitCode !== 0) throw new CommandExitError(result);
  return result;
}

const seconds = (ms: number | undefined): number | undefined => (ms ? ms / 1_000 : undefined);

async function toBytes(data: UploadData): Promise<Uint8Array> {
  if (typeof data === "string") return new TextEncoder().encode(data);
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (data instanceof Blob) return new Uint8Array(await data.arrayBuffer());
  const chunks: Uint8Array[] = [];
  for await (const chunk of data as AsyncIterable<Uint8Array>) chunks.push(chunk);
  return new Uint8Array(Buffer.concat(chunks));
}

/** A command started with `commands.run(cmd, { background: true })`. */
export class CommandHandle {
  #disconnected = false;

  constructor(
    readonly pid: number,
    private readonly process: Process,
    private readonly onStdout: OutputCallback | undefined,
    private readonly onStderr: OutputCallback | undefined,
  ) {}

  /** @internal */
  static async of(process: Process, onStdout?: OutputCallback, onStderr?: OutputCallback): Promise<CommandHandle> {
    return guardUnsupported(new CommandHandle(await process.pid(), process, onStdout, onStderr), ERRORS, "CommandHandle");
  }

  /** Output fetched so far; getters cannot fetch, so it grows while wait() runs. */
  get stdout(): string {
    return this.process.stdout;
  }

  get stderr(): string {
    return this.process.stderr;
  }

  get exitCode(): number | undefined {
    return this.process.exitCode;
  }

  /** Wait for the command, delivering output to the callbacks as it arrives until disconnected. */
  async wait(): Promise<CommandResult> {
    const deliver = (callback: OutputCallback | undefined) => (data: string) => (this.#disconnected ? undefined : callback?.(data));
    return checked(await this.process.wait(deliver(this.onStdout), deliver(this.onStderr)));
  }

  /** Stop delivering output; the command keeps running. Reconnect with `commands.connect`. */
  async disconnect(): Promise<void> {
    this.#disconnected = true;
  }

  async kill(): Promise<boolean> {
    return this.process.kill();
  }

  async sendStdin(data: string | Uint8Array, _opts?: object): Promise<void> {
    await this.process.sendStdin(data);
  }

  async closeStdin(_opts?: object): Promise<void> {
    await this.process.closeStdin();
  }
}

/** A terminal from `sandbox.pty.create`; its output goes to `onData`. */
export class PtyHandle {
  readonly pid: number;
  readonly #done: Promise<void>;

  constructor(
    private readonly terminal: Terminal,
    onData: ((data: Uint8Array) => void | Promise<void>) | undefined,
  ) {
    this.pid = terminal.pid;
    this.#done = (async () => {
      for await (const chunk of terminal.output()) await onData?.(chunk);
    })();
  }

  /** Wait for the shell to exit; a non-zero exit throws {@link CommandExitError}. */
  async wait(): Promise<CommandResult> {
    await this.#done;
    return checked(await this.terminal.wait());
  }

  async kill(): Promise<boolean> {
    return this.terminal.kill();
  }

  async disconnect(): Promise<void> {
    this.terminal.detach();
  }
}

/** Input for a command or terminal, sent as it is written. */
export class CommandInputHandle {
  constructor(private readonly write: (data: string | Uint8Array) => Promise<void>) {}

  async send(data: string | Uint8Array): Promise<void> {
    await this.write(data);
  }

  async close(): Promise<void> {}
}

/** Changes under a directory, found by comparing listings: a rename reads as a remove and a create. */
export class WatchHandle {
  #stopped = false;

  /** @internal */
  constructor(
    sandbox: CompatSandbox,
    path: string,
    onEvent: (event: FilesystemEvent) => void | Promise<void>,
    opts: WatchOpts,
  ) {
    const deadline = opts.timeoutMs === 0 ? Infinity : Date.now() + (opts.timeoutMs ?? 60_000);
    const event = (change: Change): FilesystemEvent => ({
      name: change.path.slice(path.length).replace(/^\/+/, ""),
      type: change.kind as FilesystemEventType,
    });
    void (async () => {
      let error: Error | undefined;
      try {
        let [, entries] = await sandbox.changes(path, new Map(), Boolean(opts.recursive));
        while (!this.#stopped && Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, WATCH_INTERVAL_MS));
          const [changes, after] = await sandbox.changes(path, entries, Boolean(opts.recursive));
          entries = after;
          for (const change of changes) if (!this.#stopped) await onEvent(event(change));
        }
      } catch (caught) {
        error = caught as Error;
      }
      await opts.onExit?.(error);
    })();
  }

  async stop(): Promise<void> {
    this.#stopped = true;
  }
}

/** Port exposures getHost started; the next call waits on them and reports a failure once. */
class PendingExposures {
  #pending: Promise<unknown>[] = [];

  add(exposure: Promise<unknown>): void {
    // Observed here so a failure is reported by the next call, not as an unhandled rejection.
    exposure.catch(() => undefined);
    this.#pending.push(exposure);
  }

  async settled(): Promise<void> {
    const pending = this.#pending;
    this.#pending = [];
    await Promise.all(pending);
  }
}

class Commands {
  constructor(
    private readonly sandbox: CompatSandbox,
    private readonly exposures: PendingExposures,
  ) {}

  async run(cmd: string, opts: CommandStartOpts & { background: true }): Promise<CommandHandle>;
  async run(cmd: string, opts?: CommandStartOpts & { background?: false }): Promise<CommandResult>;
  async run(cmd: string, opts: CommandStartOpts = {}): Promise<CommandResult | CommandHandle> {
    const { background, cwd, user, envs, onStdout, onStderr, stdin, shell, timeoutMs, retainOutput: _retainOutput, ...rest } = opts;
    checkOptions("commands.run", { ...rest, shell, user });
    await this.exposures.settled();
    const options = { cwd: cwd ?? HOME, envs, timeout: seconds(timeoutMs ?? DEFAULT_COMMAND_TIMEOUT_MS) };
    if (background || stdin) {
      const handle = await CommandHandle.of(await this.sandbox.start(cmd, { ...options, stdin: Boolean(stdin) }), onStdout, onStderr);
      return background ? handle : handle.wait();
    }
    const result = await this.sandbox.run(cmd, options);
    if (result.stdout) await onStdout?.(result.stdout);
    if (result.stderr) await onStderr?.(result.stderr);
    return checked(result);
  }

  /** Commands and terminals started through this layer that are still running. */
  async list(_opts?: object): Promise<ProcessInfo[]> {
    return (await this.sandbox.processes()).map((info) => ({
      pid: info.pid,
      cmd: "/bin/bash",
      args: info.pty ? ["-i", "-l"] : ["-l", "-c", info.cmd],
      envs: info.envs,
      ...(info.cwd ? { cwd: info.cwd } : {}),
    }));
  }

  async kill(pid: number, _opts?: object): Promise<boolean> {
    return this.sandbox.killProcess(pid);
  }

  async connect(pid: number, opts: { onStdout?: OutputCallback; onStderr?: OutputCallback; [option: string]: unknown } = {}): Promise<CommandHandle> {
    const process = await this.sandbox.attach(pid);
    if (!process) throw new NotFoundError(`Process with pid ${pid} not found`);
    return CommandHandle.of(process, opts.onStdout, opts.onStderr);
  }

  async sendStdin(pid: number, data: string | Uint8Array, _opts?: object): Promise<void> {
    piped(await this.sandbox.sendStdin(await this.#session(pid), data), pid);
  }

  streamInput(pid: number, _opts?: object): CommandInputHandle {
    return new CommandInputHandle((data) => this.sendStdin(pid, data));
  }

  async closeStdin(pid: number, _opts?: object): Promise<void> {
    piped(await this.sandbox.closeStdin(await this.#session(pid)), pid);
  }

  async #session(pid: number): Promise<string> {
    const info = await this.sandbox.findProcess(pid);
    if (!info) throw new NotFoundError(`Process with pid ${pid} not found`);
    return info.sessionId;
  }
}

function piped(done: boolean, pid: number): void {
  if (!done) throw new InvalidArgumentError(`Process ${pid} has no open stdin; start it with stdin: true.`);
}

/** Interactive terminals, each an Arker PTY session. */
class Pty {
  readonly #terminals = new Map<number, Terminal>();

  constructor(private readonly sandbox: CompatSandbox) {}

  async create(opts: PtyCreateOpts): Promise<PtyHandle> {
    const { cols, rows, onData, timeoutMs: _timeoutMs, user, envs, cwd, ...rest } = opts;
    checkOptions("pty.create", { ...rest, user });
    const terminal = await this.sandbox.openTerminal({
      cols,
      rows,
      cwd: cwd ?? HOME,
      envs: { TERM: "xterm-256color", LANG: "C.UTF-8", LC_ALL: "C.UTF-8", ...envs },
    });
    this.#terminals.set(terminal.pid, terminal);
    return guardUnsupported(new PtyHandle(terminal, onData), ERRORS, "CommandHandle");
  }

  async connect(pid: number, opts: { onData?: (data: Uint8Array) => void | Promise<void>; [option: string]: unknown } = {}): Promise<PtyHandle> {
    const info = await this.sandbox.findProcess(pid);
    if (!info?.pty) throw new NotFoundError(`PTY with pid ${pid} not found`);
    const terminal = await this.sandbox.attachTerminal(info);
    this.#terminals.set(pid, terminal);
    return guardUnsupported(new PtyHandle(terminal, opts.onData), ERRORS, "CommandHandle");
  }

  async sendInput(pid: number, data: Uint8Array, _opts?: object): Promise<void> {
    (await this.#terminal(pid)).send(data);
  }

  streamInput(pid: number, _opts?: object): CommandInputHandle {
    return new CommandInputHandle(async (data) => (await this.#terminal(pid)).send(data));
  }

  async resize(pid: number, size: { cols: number; rows: number }, _opts?: object): Promise<void> {
    await (await this.#terminal(pid)).resize(size.cols, size.rows);
  }

  async kill(pid: number, _opts?: object): Promise<boolean> {
    this.#terminals.delete(pid);
    return this.sandbox.killProcess(pid);
  }

  async #terminal(pid: number): Promise<Terminal> {
    const known = this.#terminals.get(pid);
    if (known) return known;
    const info = await this.sandbox.findProcess(pid);
    if (!info?.pty) throw new NotFoundError(`PTY with pid ${pid} not found`);
    const terminal = await this.sandbox.attachTerminal(info);
    this.#terminals.set(pid, terminal);
    return terminal;
  }
}

class Filesystem {
  constructor(
    private readonly sandbox: CompatSandbox,
    private readonly exposures: PendingExposures,
  ) {}

  async read(path: string, opts?: FilesystemRequestOpts & { format?: "text" }): Promise<string>;
  async read(path: string, opts: FilesystemRequestOpts & { format: "bytes" }): Promise<Uint8Array>;
  async read(path: string, opts: FilesystemRequestOpts & { format: "blob" }): Promise<Blob>;
  async read(path: string, opts: FilesystemRequestOpts & { format: "stream" }): Promise<ReadableStream<Uint8Array>>;
  async read(
    path: string,
    opts: FilesystemRequestOpts & { format?: string } = {},
  ): Promise<string | Uint8Array | Blob | ReadableStream<Uint8Array>> {
    const { format, ...rest } = opts;
    checkOptions("files.read", rest);
    await this.exposures.settled();
    const data = await this.sandbox.read(absolute(path));
    if (format === "bytes") return data;
    if (format === "blob") return new Blob([new Uint8Array(data)]);
    if (format === "stream") return new Blob([new Uint8Array(data)]).stream();
    return new TextDecoder().decode(data);
  }

  async write(path: string, data: UploadData, opts?: FilesystemRequestOpts): Promise<WriteInfo>;
  async write(files: WriteEntry[], opts?: FilesystemRequestOpts): Promise<WriteInfo[]>;
  async write(
    pathOrFiles: string | WriteEntry[],
    dataOrOpts?: UploadData | FilesystemRequestOpts,
    maybeOpts?: FilesystemRequestOpts,
  ): Promise<WriteInfo | WriteInfo[]> {
    if (Array.isArray(pathOrFiles)) return this.writeFiles(pathOrFiles, dataOrOpts as FilesystemRequestOpts);
    checkOptions("files.write", maybeOpts);
    await this.exposures.settled();
    const full = absolute(pathOrFiles);
    await this.sandbox.write(full, await toBytes((dataOrOpts as UploadData | undefined) ?? ""));
    return { name: basename(full), type: FileType.FILE, path: full };
  }

  async writeFiles(files: WriteEntry[], opts?: FilesystemRequestOpts): Promise<WriteInfo[]> {
    checkOptions("files.writeFiles", opts);
    const written: WriteInfo[] = [];
    for (const file of files) written.push(await this.write(file.path, file.data));
    return written;
  }

  async list(path: string, opts: FilesystemRequestOpts & { depth?: number } = {}): Promise<EntryInfo[]> {
    const { depth, ...rest } = opts;
    checkOptions("files.list", rest);
    await this.exposures.settled();
    return (await this.sandbox.list(absolute(path), depth ?? 1)).map(entryInfo);
  }

  async exists(path: string, opts?: FilesystemRequestOpts): Promise<boolean> {
    checkOptions("files.exists", opts);
    await this.exposures.settled();
    return this.sandbox.pathExists(absolute(path));
  }

  async getInfo(path: string, opts?: FilesystemRequestOpts): Promise<EntryInfo> {
    checkOptions("files.getInfo", opts);
    await this.exposures.settled();
    return entryInfo(await this.sandbox.stat(absolute(path)));
  }

  /** Create `path` and its parents; false if it already existed. */
  async makeDir(path: string, opts?: FilesystemRequestOpts): Promise<boolean> {
    checkOptions("files.makeDir", opts);
    await this.exposures.settled();
    return this.sandbox.makeDir(absolute(path));
  }

  async remove(path: string, opts?: FilesystemRequestOpts): Promise<void> {
    checkOptions("files.remove", opts);
    await this.exposures.settled();
    await this.sandbox.remove(absolute(path));
  }

  async rename(oldPath: string, newPath: string, opts?: FilesystemRequestOpts): Promise<EntryInfo> {
    checkOptions("files.rename", opts);
    await this.exposures.settled();
    const full = absolute(newPath);
    await this.sandbox.rename(absolute(oldPath), full);
    return entryInfo(await this.sandbox.stat(full));
  }

  /** Watch by polling: changes are found by comparing a fresh listing with the last. */
  async watchDir(path: string, onEvent: (event: FilesystemEvent) => void | Promise<void>, opts: WatchOpts = {}): Promise<WatchHandle> {
    const { timeoutMs: _timeoutMs, onExit: _onExit, recursive: _recursive, ...rest } = opts;
    checkOptions("files.watchDir", rest);
    await this.exposures.settled();
    const full = absolute(path);
    if (!(await this.sandbox.isDir(full))) throw new NotFoundError(`Directory not found: ${full}`);
    return guardUnsupported(new WatchHandle(this.sandbox, full, onEvent, opts), ERRORS, "WatchHandle");
  }
}

export class GitAuthError extends AuthenticationError {}
export class GitUpstreamError extends SandboxError {}

export type GitResetMode = "soft" | "mixed" | "hard" | "merge" | "keep";
export type GitStatusLabel = "modified" | "added" | "deleted" | "renamed" | "copied" | "untracked" | "typechange" | "conflict" | "unknown";

export interface GitFileStatus {
  name: string;
  status: GitStatusLabel;
  indexStatus: string;
  workingTreeStatus: string;
  staged: boolean;
  renamedFrom?: string;
}

export interface GitStatus {
  currentBranch?: string;
  upstream?: string;
  ahead: number;
  behind: number;
  detached: boolean;
  fileStatus: GitFileStatus[];
  isClean: boolean;
  hasChanges: boolean;
  hasStaged: boolean;
  hasUntracked: boolean;
  hasConflicts: boolean;
  totalCount: number;
  stagedCount: number;
  unstagedCount: number;
  untrackedCount: number;
  conflictCount: number;
}

export interface GitBranches {
  branches: string[];
  currentBranch?: string;
}

export interface GitRequestOpts {
  envs?: Record<string, string>;
  /** Accepted for compatibility; git runs as root. */
  user?: string;
  cwd?: string;
  timeoutMs?: number;
  requestTimeoutMs?: number;
  [option: string]: unknown;
}

// Words in git's output that mean it was refused credentials, or has no upstream to use.
const GIT_AUTH_FAILURES = [
  "authentication failed",
  "terminal prompts disabled",
  "could not read username",
  "invalid username or password",
  "access denied",
  "permission denied",
  "not authorized",
];
const GIT_NO_UPSTREAM = ["no upstream", "no tracking information", "please specify which branch"];

function gitFailure(result: CommandResult): Error {
  const output = `${result.stderr}\n${result.stdout}`.toLowerCase();
  if (GIT_AUTH_FAILURES.some((snippet) => output.includes(snippet))) return new GitAuthError(result.stderr.trim() || "Git authentication failed.");
  if (GIT_NO_UPSTREAM.some((snippet) => output.includes(snippet))) return new GitUpstreamError(result.stderr.trim());
  return new CommandExitError(result);
}

// A porcelain status letter's name; the first that applies to either column wins.
const GIT_FILE_STATES: [string, GitStatusLabel][] = [
  ["U", "conflict"],
  ["R", "renamed"],
  ["C", "copied"],
  ["D", "deleted"],
  ["A", "added"],
  ["M", "modified"],
  ["T", "typechange"],
  ["?", "untracked"],
];

function fileStatus(change: GitFileChange): GitFileStatus {
  const letters = [change.indexStatus, change.worktreeStatus];
  const status = GIT_FILE_STATES.find(([letter]) => letters.includes(letter))?.[1] ?? "unknown";
  return {
    name: change.name,
    status,
    indexStatus: change.indexStatus,
    workingTreeStatus: change.worktreeStatus,
    staged: change.indexStatus !== " " && change.indexStatus !== "?",
    ...(change.renamedFrom ? { renamedFrom: change.renamedFrom } : {}),
  };
}

function gitStatus(state: GitState): GitStatus {
  const files = state.files.map(fileStatus);
  const count = (test: (file: GitFileStatus) => boolean) => files.filter(test).length;
  return {
    ...(state.currentBranch ? { currentBranch: state.currentBranch } : {}),
    ...(state.upstream ? { upstream: state.upstream } : {}),
    ahead: state.ahead,
    behind: state.behind,
    detached: state.detached,
    fileStatus: files,
    isClean: files.length === 0,
    hasChanges: files.length > 0,
    hasStaged: count((file) => file.staged) > 0,
    hasUntracked: count((file) => file.status === "untracked") > 0,
    hasConflicts: count((file) => file.status === "conflict") > 0,
    totalCount: files.length,
    stagedCount: count((file) => file.staged),
    unstagedCount: count((file) => !file.staged),
    untrackedCount: count((file) => file.status === "untracked"),
    conflictCount: count((file) => file.status === "conflict"),
  };
}

/** E2B's per-call options: `envs`, `cwd` and `timeoutMs` apply; `user` and `requestTimeoutMs` do not. */
function gitOptions(method: string, opts: GitRequestOpts): GitOptions {
  const { envs, cwd, timeoutMs, user: _user, requestTimeoutMs: _requestTimeoutMs, ...rest } = opts;
  rejectOptions(ERRORS, `git.${method}`, rest);
  return { ...(envs ? { envs } : {}), ...(cwd ? { cwd } : {}), ...(timeoutMs ? { timeout: timeoutMs / 1_000 } : {}) };
}

function credentials(username?: string, password?: string): { username?: string; password?: string } {
  return username || password ? { username, password } : {};
}

/** Git in the sandbox, run as the git CLI. Relative paths are under /home/user. */
export class Git {
  readonly #git: CompatGit;

  constructor(sandbox: CompatSandbox) {
    this.#git = new CompatGit(sandbox, HOME, gitFailure);
  }

  async clone(
    url: string,
    opts: GitRequestOpts & { path?: string; branch?: string; depth?: number; username?: string; password?: string; dangerouslyStoreCredentials?: boolean } = {},
  ): Promise<CommandResult> {
    const { path, branch, depth, username, password, dangerouslyStoreCredentials, ...rest } = opts;
    if (username && !password) throw new InvalidArgumentError("A password or token is required with a username.");
    const result = await this.#git.clone(url, path, { branch, depth, ...credentials(username, password), ...gitOptions("clone", rest) });
    if (dangerouslyStoreCredentials && username && password) {
      const { host, protocol } = new URL(url);
      await this.dangerouslyAuthenticate({ username, password, host, protocol: protocol.replace(/:$/, ""), ...rest });
    }
    return result;
  }

  async init(path: string, opts: GitRequestOpts & { bare?: boolean; initialBranch?: string } = {}): Promise<CommandResult> {
    const { bare, initialBranch, ...rest } = opts;
    return this.#git.init(path, { bare, initialBranch, ...gitOptions("init", rest) });
  }

  async remoteAdd(path: string, name: string, url: string, opts: GitRequestOpts & { fetch?: boolean; overwrite?: boolean } = {}): Promise<CommandResult> {
    const { fetch, overwrite, ...rest } = opts;
    return this.#git.remoteAdd(path, name, url, { fetch, overwrite, ...gitOptions("remoteAdd", rest) });
  }

  async remoteGet(path: string, name: string, opts: GitRequestOpts = {}): Promise<string | undefined> {
    return this.#git.remoteGet(path, name, gitOptions("remoteGet", opts));
  }

  async status(path: string, opts: GitRequestOpts = {}): Promise<GitStatus> {
    return gitStatus(await this.#git.status(path, gitOptions("status", opts)));
  }

  async branches(path: string, opts: GitRequestOpts = {}): Promise<GitBranches> {
    const { branches, current } = await this.#git.branches(path, gitOptions("branches", opts));
    return { branches, ...(current ? { currentBranch: current } : {}) };
  }

  async createBranch(path: string, branch: string, opts: GitRequestOpts = {}): Promise<CommandResult> {
    return this.#git.createBranch(path, branch, gitOptions("createBranch", opts));
  }

  async checkoutBranch(path: string, branch: string, opts: GitRequestOpts = {}): Promise<CommandResult> {
    return this.#git.checkoutBranch(path, branch, gitOptions("checkoutBranch", opts));
  }

  async deleteBranch(path: string, branch: string, opts: GitRequestOpts & { force?: boolean } = {}): Promise<CommandResult> {
    const { force, ...rest } = opts;
    return this.#git.deleteBranch(path, branch, { force, ...gitOptions("deleteBranch", rest) });
  }

  async add(path: string, opts: GitRequestOpts & { files?: string[]; all?: boolean } = {}): Promise<CommandResult> {
    const { files, all: _all, ...rest } = opts;
    return this.#git.add(path, files, gitOptions("add", rest));
  }

  async commit(
    path: string,
    message: string,
    opts: GitRequestOpts & { authorName?: string; authorEmail?: string; allowEmpty?: boolean } = {},
  ): Promise<CommandResult> {
    const { authorName, authorEmail, allowEmpty, ...rest } = opts;
    return this.#git.commit(path, message, { authorName, authorEmail, allowEmpty, ...gitOptions("commit", rest) });
  }

  async reset(path: string, opts: GitRequestOpts & { mode?: GitResetMode; target?: string; paths?: string[] } = {}): Promise<CommandResult> {
    const { mode, target, paths, ...rest } = opts;
    return this.#git.reset(path, { mode, target, paths, ...gitOptions("reset", rest) });
  }

  async restore(path: string, opts: GitRequestOpts & { paths: string[]; staged?: boolean; worktree?: boolean; source?: string }): Promise<CommandResult> {
    const { paths, staged, worktree, source, ...rest } = opts;
    return this.#git.restore(path, paths, { staged, worktree, source, ...gitOptions("restore", rest) });
  }

  async push(
    path: string,
    opts: GitRequestOpts & { remote?: string; branch?: string; setUpstream?: boolean; username?: string; password?: string } = {},
  ): Promise<CommandResult> {
    const { remote, branch, setUpstream = true, username, password, ...rest } = opts;
    const options = gitOptions("push", rest);
    const upstream = setUpstream && !(await this.#git.status(path, options)).upstream;
    return this.#git.push(path, { remote, branch, setUpstream: upstream, ...credentials(username, password), ...options });
  }

  async pull(path: string, opts: GitRequestOpts & { remote?: string; branch?: string; username?: string; password?: string } = {}): Promise<CommandResult> {
    const { remote, branch, username, password, ...rest } = opts;
    return this.#git.pull(path, { remote, branch, ...credentials(username, password), ...gitOptions("pull", rest) });
  }

  async setConfig(key: string, value: string, opts: GitRequestOpts & { scope?: string; path?: string } = {}): Promise<CommandResult> {
    const { scope, path, ...rest } = opts;
    return this.#git.setConfig(key, value, { scope, path, ...gitOptions("setConfig", rest) });
  }

  async getConfig(key: string, opts: GitRequestOpts & { scope?: string; path?: string } = {}): Promise<string | undefined> {
    const { scope, path, ...rest } = opts;
    return this.#git.getConfig(key, { scope, path, ...gitOptions("getConfig", rest) });
  }

  async dangerouslyAuthenticate(opts: GitRequestOpts & { username: string; password: string; host?: string; protocol?: string }): Promise<CommandResult> {
    const { username, password, host, protocol, ...rest } = opts;
    return this.#git.dangerouslyAuthenticate(username, password, { host, protocol, ...gitOptions("dangerouslyAuthenticate", rest) });
  }

  async configureUser(name: string, email: string, opts: GitRequestOpts & { scope?: string; path?: string } = {}): Promise<CommandResult> {
    const { scope, path, ...rest } = opts;
    return this.#git.configureUser(name, email, { scope, path, ...gitOptions("configureUser", rest) });
  }
}

export class VolumeError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = new.target.name;
  }
}
export class VolumeNotFoundError extends VolumeError {}

const VOLUME_ERRORS: Errors = { ...ERRORS, notFound: VolumeNotFoundError, invalid: VolumeError };
const VOLUME_FILES_HINT = "mount the volume with Sandbox.create({ volumeMounts: { [path]: volume } }) and use sandbox.files.";

export interface VolumeInfo {
  volumeId: string;
  name: string;
}

export interface VolumeOpts {
  /** Where and how to reach Arker. */
  arker?: CompatConfig;
  [option: string]: unknown;
}

/** A persistent volume, as an Arker filesystem: mount it into sandboxes with `volumeMounts`. Its files are read and written through a sandbox. */
export class Volume {
  readonly token = "";

  constructor(
    readonly volumeId: string,
    readonly name: string,
  ) {
    return guardUnsupported(this, ERRORS, "Volume", {
      list: VOLUME_FILES_HINT,
      makeDir: VOLUME_FILES_HINT,
      getInfo: VOLUME_FILES_HINT,
      exists: VOLUME_FILES_HINT,
      updateMetadata: VOLUME_FILES_HINT,
      readFile: VOLUME_FILES_HINT,
      writeFile: VOLUME_FILES_HINT,
      remove: VOLUME_FILES_HINT,
    });
  }

  static async create(name: string, opts: VolumeOpts = {}): Promise<Volume> {
    const volume = await new Volumes(VOLUME_ERRORS, opts.arker).create(name);
    return new Volume(volume.filesystem_id, volume.name);
  }

  static async connect(volumeId: string, opts: VolumeOpts = {}): Promise<Volume> {
    const volume = await new Volumes(VOLUME_ERRORS, opts.arker).byId(volumeId);
    return new Volume(volume.filesystem_id, volume.name);
  }

  static async getInfo(volumeId: string, opts: VolumeOpts = {}): Promise<VolumeInfo> {
    const volume = await new Volumes(VOLUME_ERRORS, opts.arker).byId(volumeId);
    return { volumeId: volume.filesystem_id, name: volume.name };
  }

  static async list(opts: VolumeOpts = {}): Promise<VolumeInfo[]> {
    return (await new Volumes(VOLUME_ERRORS, opts.arker).list()).map((volume) => ({ volumeId: volume.filesystem_id, name: volume.name }));
  }

  static async destroy(volumeId: string, opts: VolumeOpts = {}): Promise<boolean> {
    return new Volumes(VOLUME_ERRORS, opts.arker).delete(volumeId);
  }
}

/** Mount path to volume id, for volumes given by object or by name. */
async function volumeIds(volumeMounts: Record<string, Volume | string> | undefined, arker: CompatConfig | undefined): Promise<Record<string, string> | undefined> {
  if (!volumeMounts) return undefined;
  const volumes = new Volumes(VOLUME_ERRORS, arker);
  const ids: Record<string, string> = {};
  for (const [path, volume] of Object.entries(volumeMounts)) {
    ids[path] = volume instanceof Volume ? volume.volumeId : (await volumes.get(volume)).filesystem_id;
  }
  return ids;
}

/**
 * An E2B sandbox running as an Arker VM.
 *
 * Like E2B, a sandbox is deleted `timeoutMs` (default 300 000) after it is
 * created; extend it with {@link Sandbox.setTimeout}. `lifecycle: { onTimeout:
 * "pause" }` keeps it instead, as does {@link Sandbox.pause}.
 */
export class Sandbox {
  readonly sandboxId: string;
  readonly commands: Commands;
  readonly files: Filesystem;
  readonly git: Git;
  readonly pty: Pty;
  readonly #sandbox: CompatSandbox;
  readonly #exposures = new PendingExposures();
  readonly #guarded: Sandbox;

  protected constructor(sandbox: CompatSandbox) {
    this.#sandbox = sandbox;
    this.sandboxId = sandbox.id;
    this.commands = guardUnsupported(new Commands(sandbox, this.#exposures), ERRORS, "sandbox.commands");
    this.files = guardUnsupported(new Filesystem(sandbox, this.#exposures), ERRORS, "sandbox.files");
    this.git = guardUnsupported(new Git(sandbox), ERRORS, "sandbox.git");
    this.pty = guardUnsupported(new Pty(sandbox), ERRORS, "sandbox.pty");
    this.#guarded = guardUnsupported(this, ERRORS, "Sandbox");
    return this.#guarded;
  }

  static async create(opts?: SandboxOpts): Promise<Sandbox>;
  static async create(template: string, opts?: SandboxOpts): Promise<Sandbox>;
  static async create(templateOrOpts?: string | SandboxOpts, maybeOpts: SandboxOpts = {}): Promise<Sandbox> {
    const opts = typeof templateOrOpts === "object" ? templateOrOpts : maybeOpts;
    const template = typeof templateOrOpts === "string" ? templateOrOpts : opts.template;
    const { template: _template, timeoutMs, metadata, envs, cpuCount, allowInternetAccess, network, lifecycle, volumeMounts, arker, ...rest } = opts;
    rejectOptions(ERRORS, "Sandbox.create", rest, E2B_CONNECTION_OPTIONS);
    const sandbox = await CompatSandbox.create(ERRORS, {
      config: arker,
      source: template === undefined || template === "base" ? undefined : template,
      envs: { E2B_SANDBOX: "true", ...envs },
      cpus: cpuCount,
      deleteAfter: seconds(timeoutMs || DEFAULT_SANDBOX_TIMEOUT_MS),
      onTimeout: lifecycle?.onTimeout === "pause" ? "suspend" : "delete",
      egress: egressFor(network, allowInternetAccess),
      metadata,
      idEnv: "E2B_SANDBOX_ID",
      volumes: await volumeIds(volumeMounts, arker),
      portAuth: network?.allowPublicTraffic === false ? newPortAuth(TRAFFIC_TOKEN_HEADER) : undefined,
    });
    return new this(sandbox);
  }

  /**
   * The sandboxes this layer created, filtered by `query`, oldest first unless `order` is `"desc"`.
   * A paginator's `nextToken` continues the listing from where its last page ended.
   */
  static list(opts: SandboxListOpts = {}): SandboxPaginator {
    const { query = {}, order, limit, nextToken, arker, ...rest } = opts;
    rejectOptions(ERRORS, "Sandbox.list", rest, E2B_CONNECTION_OPTIONS);
    if (order !== undefined && order !== "asc" && order !== "desc") {
      throw new InvalidArgumentError(`Invalid order '${order}', expected 'asc' or 'desc'`);
    }
    const direction = order === "desc" ? -1 : 1;
    const after = nextToken ? afterToken(nextToken) : undefined;
    async function* listed(): AsyncGenerator<SandboxInfo> {
      const found: SandboxInfo[] = [];
      for await (const sandbox of CompatSandbox.find(ERRORS, arker)) {
        if (matches(sandbox, query)) found.push(sandboxInfo(sandbox.vm, sandbox.settings));
      }
      found.sort((a, b) => listKey(a).localeCompare(listKey(b)) * direction);
      yield* found.filter((info) => after === undefined || listKey(info).localeCompare(after) * direction > 0);
    }
    return new SandboxPaginator(listed(), limit, listToken);
  }

  private static async byId(sandboxId: string, opts: SandboxApiOpts = {}): Promise<Sandbox> {
    const compat = await CompatSandbox.connect(ERRORS, sandboxId, opts.arker);
    if (!compat) throw new SandboxNotFoundError(`Sandbox ${sandboxId} not found`);
    return new Sandbox(compat);
  }

  static async pause(sandboxId: string, opts: SandboxPauseOpts = {}): Promise<boolean> {
    return (await Sandbox.byId(sandboxId, opts)).pause(opts);
  }

  static async fork(sandboxId: string, opts: SandboxForkOpts = {}): Promise<Array<Sandbox | Error>> {
    return (await Sandbox.byId(sandboxId, opts)).fork(opts);
  }

  static async createSnapshot(sandboxId: string, opts: CreateSnapshotOpts = {}): Promise<SnapshotInfo> {
    return (await Sandbox.byId(sandboxId, opts)).createSnapshot(opts);
  }

  static listSnapshots(opts: SnapshotListOpts = {}): SnapshotPaginator {
    const { sandboxId, name, limit, arker, ...rest } = opts;
    rejectOptions(ERRORS, "Sandbox.listSnapshots", rest, E2B_CONNECTION_OPTIONS);
    async function* listed(): AsyncGenerator<SnapshotInfo> {
      for await (const snapshot of CompatSandbox.find(ERRORS, arker, { snapshots: true })) {
        if ((sandboxId === undefined || snapshot.snapshotOf === sandboxId) && (name === undefined || snapshot.vm.name === name)) {
          yield snapshotInfo(snapshot);
        }
      }
    }
    return new SnapshotPaginator(listed(), limit);
  }

  /** Delete a snapshot; false when there is no such snapshot. */
  static async deleteSnapshot(snapshotId: string, opts: SandboxApiOpts = {}): Promise<boolean> {
    const snapshot = await CompatSandbox.connect(ERRORS, snapshotId, opts.arker);
    return snapshot !== null && snapshot.snapshotOf !== undefined && snapshot.delete();
  }

  static async updateNetwork(sandboxId: string, network: SandboxNetworkOpts, opts: SandboxApiOpts = {}): Promise<void> {
    return (await Sandbox.byId(sandboxId, opts)).updateNetwork(network);
  }

  static getMetrics(_sandboxId: string, _opts?: unknown): never {
    throw unsupported(ERRORS, "Sandbox.getMetrics");
  }

  /** The Arker VM behind this sandbox, for anything this layer does not cover. */
  get arker(): VM {
    return this.#sandbox.vm;
  }

  /** @internal */
  protected get compat(): CompatSandbox {
    return this.#sandbox;
  }

  /**
   * Reconnect to a sandbox. An id Arker does not know is handed to the `e2b`
   * package when it is installed, so existing E2B sandboxes keep working.
   */
  static async connect(sandboxId: string, opts: SandboxConnectOpts = {}): Promise<Sandbox> {
    const { arker, ...nativeOpts } = opts;
    const compat = await CompatSandbox.connect(ERRORS, sandboxId, arker);
    if (!compat) {
      const native = await importOptional<{ Sandbox: { connect(id: string, opts?: object): Promise<Sandbox> } }>("e2b");
      if (!native) throw new SandboxNotFoundError(`Sandbox ${sandboxId} not found`);
      return native.Sandbox.connect(sandboxId, nativeOpts);
    }
    return new this(compat).connect(opts);
  }

  /** Resume this sandbox; Arker VMs resume on their own when used, memory included. */
  async connect(opts: { timeoutMs?: number; onResume?: "restore" | "reboot" } = {}): Promise<Sandbox> {
    if (opts.onResume !== undefined && opts.onResume !== "restore") {
      throw unsupported(ERRORS, `connect({ onResume: '${opts.onResume}' })`, "Arker resumes a sandbox with its memory.");
    }
    if (opts.timeoutMs) await this.setTimeout(opts.timeoutMs);
    return this.#guarded;
  }

  /** Delete a sandbox by id. An id Arker does not know is handed to the `e2b` package when it is installed. */
  static async kill(sandboxId: string, opts: SandboxConnectOpts = {}): Promise<boolean> {
    const { arker, ...nativeOpts } = opts;
    const compat = await CompatSandbox.connect(ERRORS, sandboxId, arker);
    if (compat) return compat.delete();
    const native = await importOptional<{ Sandbox: { kill(id: string, opts?: object): Promise<boolean> } }>("e2b");
    return native ? native.Sandbox.kill(sandboxId, nativeOpts) : false;
  }

  async kill(): Promise<boolean> {
    return this.#sandbox.delete();
  }

  /** Delete (or, with `onTimeout: "pause"`, keep) the sandbox `timeoutMs` from now. */
  async setTimeout(timeoutMs: number): Promise<void> {
    await this.#sandbox.setTimeout(timeoutMs / 1_000);
  }

  /**
   * Suspend the sandbox and cancel its timeout. Arker keeps memory as well as
   * files, whatever `mode` asks. Resume with {@link Sandbox.connect}.
   */
  async pause(opts: SandboxPauseOpts = {}): Promise<boolean> {
    const { keepMemory, mode, arker: _arker, ...rest } = opts;
    rejectOptions(
      ERRORS,
      "pause",
      { keepMemory: keepMemory === false ? false : undefined, mode: mode === "full" ? undefined : mode, ...rest },
      new Set([...E2B_CONNECTION_OPTIONS, "strict"]),
      new Set(["keepMemory", "mode"]),
    );
    await this.#sandbox.clearTimeout();
    await this.#sandbox.suspend();
    return true;
  }

  async betaPause(opts: SandboxPauseOpts = {}): Promise<boolean> {
    return this.pause(opts);
  }

  /** New sandboxes from this one's files and memory; a failed fork is an item, not an error. */
  async fork(opts: SandboxForkOpts = {}): Promise<Array<Sandbox | Error>> {
    const forks: Array<Sandbox | Error> = [];
    for (let index = 0; index < (opts.count ?? 1); index++) {
      try {
        const compat = await this.#sandbox.fork({ deleteAfter: seconds(opts.timeoutMs || DEFAULT_SANDBOX_TIMEOUT_MS), idEnv: "E2B_SANDBOX_ID" });
        forks.push(new Sandbox(compat));
      } catch (error) {
        if (!(error instanceof SandboxError)) throw error;
        forks.push(error);
      }
    }
    return forks;
  }

  /** Snapshot the sandbox's files and memory. Start a sandbox from it with `Sandbox.create(snapshot.snapshotId)`. */
  async createSnapshot(opts: CreateSnapshotOpts = {}): Promise<SnapshotInfo> {
    const { name, mode, arker: _arker, ...rest } = opts;
    rejectOptions(ERRORS, "createSnapshot", { mode: mode === "full" ? undefined : mode, ...rest }, E2B_CONNECTION_OPTIONS, new Set(["mode"]));
    return snapshotInfo(await this.#sandbox.snapshot(name ?? `e2b-snapshot-${crypto.randomUUID().slice(0, 12)}`));
  }

  listSnapshots(opts: Omit<SnapshotListOpts, "sandboxId"> = {}): SnapshotPaginator {
    return Sandbox.listSnapshots({ ...opts, sandboxId: this.sandboxId });
  }

  /** Replace the sandbox's outbound rules; what `network` leaves out is cleared. */
  async updateNetwork(network: SandboxNetworkOpts, _opts?: SandboxApiOpts): Promise<void> {
    const isPublic = network.allowPublicTraffic;
    if (isPublic !== undefined && isPublic !== (this.#sandbox.portAuth === undefined)) {
      throw unsupported(ERRORS, "updateNetwork({ allowPublicTraffic })", "set it with Sandbox.create({ network }).");
    }
    await this.#sandbox.updateEgress(egressFor(network));
  }

  /** The token requests to the sandbox's ports must carry, or undefined when they are public. */
  get trafficAccessToken(): string | undefined {
    return this.#sandbox.portAuth?.token;
  }

  /**
   * A URL that downloads `path` with no credentials, for `useSignatureExpiration` seconds
   * when given, else while the sandbox lives. Forks do not inherit it.
   */
  async downloadUrl(path: string, opts: { user?: string; useSignatureExpiration?: number } = {}): Promise<string> {
    checkOptions("downloadUrl", { user: opts.user });
    return this.#sandbox.fileUrl(absolute(path), { write: false, expiresIn: opts.useSignatureExpiration });
  }

  /** A URL that writes a POSTed multipart/form-data file to `path` with no credentials. */
  async uploadUrl(path?: string, opts: { user?: string; useSignatureExpiration?: number } = {}): Promise<string> {
    checkOptions("uploadUrl", { user: opts.user });
    if (!path) throw new InvalidArgumentError("uploadUrl needs the path to write to.");
    return this.#sandbox.fileUrl(absolute(path), { write: true, expiresIn: opts.useSignatureExpiration });
  }

  async isRunning(): Promise<boolean> {
    return this.#sandbox.isAlive();
  }

  async getInfo(): Promise<SandboxInfo> {
    return sandboxInfo(await this.#sandbox.info(), await this.#sandbox.metadata());
  }

  /**
   * Return the host name for `port`, synchronously as E2B does. Created with
   * `network: { allowPublicTraffic: false }`, the sandbox lets in only requests
   * carrying {@link trafficAccessToken} in the `e2b-traffic-access-token`
   * header; otherwise the port is public, as on E2B. The
   * port opens in the background: the URL serves once the next awaited call
   * on this sandbox's commands or files returns, and that call throws if
   * opening it failed.
   */
  getHost(port: number): string {
    const host = this.#sandbox.portHost(port);
    if (!host) throw new SandboxError("This Arker region does not expose guest ports.");
    this.#exposures.add(this.#sandbox.expose(port));
    return host;
  }
}

export default Sandbox;
