/**
 * Daytona-compatible sandboxes backed by Arker VMs.
 *
 * Replace `@daytonaio/sdk` imports with `@arker-ai/sdk/daytona`. What this
 * layer cannot honor throws rather than doing something else.
 */
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { Readable } from "node:stream";

import {
  blocksAllEgress,
  CodeContexts,
  CompatGit,
  CompatSandbox,
  type Egress,
  Volumes,
  type CodeContext,
  guardUnsupported,
  importOptional,
  permissions,
  rejectOptions,
  shellQuote,
  type CommandResult,
  type CompatConfig,
  type Entry,
  type Errors,
  type ProcessInfo,
  type Terminal,
} from "./compat/core.js";
import type { Filesystem, VM, Vm } from "./index.js";

const CODE_DIR = "/tmp/arker-daytona-code";
// How long Daytona's signed URLs work unless told otherwise, in seconds.
const DEFAULT_FILE_URL_TTL = 3600;
const DEFAULT_PREVIEW_URL_TTL = 60;
// Marks a snapshot made from an image rather than a sandbox.
const SNAPSHOT_OF_IMAGE = "image";

// File extension and runner per Daytona language.
const LANGUAGES: Record<string, [extension: string, runner: string]> = {
  python: ["py", "python3"],
  javascript: ["js", "node"],
  typescript: ["ts", "node --no-warnings --experimental-strip-types"],
};

// Accepted with a warning: Arker suspends idle VMs, runs as root, and places VMs itself.
const INERT_PARAMS = new Set([
  "autoArchiveInterval",
  "autoDeleteInterval",
  "autoStopInterval",
  "ephemeral",
  "user",
  "target",
  "timeout",
]);

export class DaytonaError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = new.target.name;
  }
}
export class DaytonaValidationError extends DaytonaError {}
export class DaytonaNotFoundError extends DaytonaError {}
export class DaytonaTimeoutError extends DaytonaError {}
export class DaytonaNotSupportedError extends DaytonaValidationError {}

const ERRORS: Errors = {
  provider: "Daytona",
  unsupported: DaytonaNotSupportedError,
  invalid: DaytonaValidationError,
  notFound: DaytonaNotFoundError,
  fileNotFound: DaytonaNotFoundError,
  timeout: DaytonaTimeoutError,
  failed: DaytonaError,
};

export interface DaytonaConfig {
  /** Daytona credentials, used only to reach sandboxes that still live on Daytona. */
  apiKey?: string;
  apiUrl?: string;
  target?: string;
  /** Where and how to reach Arker, and which VM to start from. */
  arker?: CompatConfig;
}

export interface Resources {
  cpu?: number;
  /** GiB */
  memory?: number;
  disk?: number;
  gpu?: number;
}

export interface CreateSandboxBaseParams {
  language?: string;
  envVars?: Record<string, string>;
  labels?: Record<string, string>;
  public?: boolean;
  resources?: Resources;
  networkBlockAll?: boolean;
  /** Comma-separated CIDR blocks the sandbox may reach; everything else is denied. */
  networkAllowList?: string;
  name?: string;
  volumes?: VolumeMount[];
  [param: string]: unknown;
}
export type CreateSandboxFromSnapshotParams = CreateSandboxBaseParams & { snapshot?: string };
export type CreateSandboxFromImageParams = CreateSandboxBaseParams & { image: string };

export interface ListSandboxesQuery {
  /** A prefix of the sandbox id. */
  id?: string;
  /** A prefix of the sandbox name. */
  name?: string;
  labels?: Record<string, string>;
  limit?: number;
  /** Every sandbox counts as started: Arker resumes a suspended one when it is used. */
  states?: string[];
  snapshots?: string[];
  isPublic?: boolean;
  minCpu?: number;
  maxCpu?: number;
  minMemoryGib?: number;
  maxMemoryGib?: number;
  minDiskGib?: number;
  maxDiskGib?: number;
  createdAtAfter?: Date;
  createdAtBefore?: Date;
  lastActivityAfter?: Date;
  lastActivityBefore?: Date;
  sort?: string;
  order?: "asc" | "desc";
  [filter: string]: unknown;
}

export type CreateSnapshotParams = { name: string; image: string | object; resources?: Resources; [param: string]: unknown };

/** A sandbox's files and memory, or a registry image, that sandboxes start from by name: an Arker VM, so it is always active. */
export interface Snapshot {
  id: string;
  name: string;
  imageName?: string;
  state: "active";
  cpu?: number;
  gpu: number;
  mem?: number;
  disk?: number;
  entrypoint: string[] | null;
  general: boolean;
  errorReason: string | null;
  createdAt: Date;
  sourceSandboxId: string | null;
}

export interface PaginatedSnapshots {
  items: Snapshot[];
  total: number;
  page: number;
  totalPages: number;
}

export interface SignedPortPreviewUrl {
  sandboxId: string;
  port: number;
  token: string;
  url: string;
}

export interface UpdateSandboxNetworkSettings {
  networkBlockAll?: boolean;
  networkAllowList?: string;
  domainAllowList?: string;
}

export interface ForkSandboxParams {
  name?: string;
}


/** Daytona's network settings: an allow list denies what it does not name. */
function egressFor(blockAll: boolean | undefined, allowList: string | undefined, domainAllowList?: string): Egress | undefined {
  if (blockAll) return { denyAll: true };
  if (allowList === undefined && domainAllowList === undefined) return undefined;
  const split = (list: string | undefined) => (list ?? "").split(",").map((entry) => entry.trim()).filter(Boolean);
  return { allowIps: split(allowList), allowHosts: split(domainAllowList), denyAll: true };
}

export interface ExecuteResponse {
  exitCode: number;
  result: string;
}

export interface CodeRunParams {
  argv?: string[];
  env?: Record<string, string>;
}

export interface FileInfo {
  name: string;
  isDir: boolean;
  size: number;
  mode: string;
  permissions: string;
  owner: string;
  group: string;
  modTime: string;
  modifiedAt?: string;
  path?: string;
}

export interface Match {
  file: string;
  line: number;
  content: string;
}

export interface SearchFilesResponse {
  files: string[];
}

export interface ReplaceResult {
  file?: string;
  success?: boolean;
  error?: string;
}

export interface FilePermissionsParams {
  mode?: string;
  owner?: string;
  group?: string;
}

export interface FileUpload {
  source: string | Buffer | Uint8Array;
  destination: string;
}

export interface FileDownloadRequest {
  source: string;
  destination?: string;
}

export interface FileDownloadResponse {
  source: string;
  result?: Buffer | string;
  error?: string;
}

export type UploadSource = Buffer | Uint8Array | string | Readable | ReadableStream<Uint8Array>;

export interface SessionExecuteRequest {
  command: string;
  runAsync?: boolean;
  async?: boolean;
  suppressInputEcho?: boolean;
}

export interface SessionExecuteResponse {
  cmdId: string;
  output?: string;
  stdout?: string;
  stderr?: string;
  exitCode?: number;
}

export interface SessionCommandLogsResponse {
  output?: string;
  stdout?: string;
  stderr?: string;
}

export interface Command {
  id: string;
  command: string;
  exitCode?: number;
}

export interface Session {
  sessionId: string;
  commands: Command[];
}

export interface PtyCreateOptions {
  id: string;
  cwd?: string;
  envs?: Record<string, string>;
  cols?: number;
  rows?: number;
}

export interface PtyConnectOptions {
  onData: (data: Uint8Array) => void | Promise<void>;
}

export interface PtyResult {
  exitCode?: number;
  error?: string;
}

export interface PtySessionInfo {
  id: string;
  cwd: string;
  envs: Record<string, string>;
  cols: number;
  rows: number;
  active: boolean;
  lazyStart: boolean;
}

function ptyInfo(info: ProcessInfo): PtySessionInfo {
  return { id: info.name!, cwd: info.cwd ?? "", envs: info.envs, cols: info.size?.cols ?? 0, rows: info.size?.rows ?? 0, active: true, lazyStart: false };
}

function fileInfo(entry: Entry): FileInfo {
  const modified = new Date(entry.modified * 1_000);
  const kind = entry.symlinkTarget ? "l" : entry.isDir ? "d" : "-";
  return {
    name: entry.name,
    isDir: entry.isDir,
    size: entry.size,
    mode: kind + permissions(entry.mode),
    permissions: entry.mode.toString(8).padStart(4, "0"),
    owner: entry.owner,
    group: entry.group,
    modTime: modified.toISOString().replace("T", " ").replace("Z", " +0000 UTC"),
    modifiedAt: modified.toISOString(),
    path: entry.path,
  };
}

async function readSource(source: UploadSource): Promise<Uint8Array> {
  if (typeof source === "string") return readFile(source);
  if (source instanceof Uint8Array) return source;
  const chunks: Uint8Array[] = [];
  for await (const chunk of source as AsyncIterable<Uint8Array>) chunks.push(chunk);
  return Buffer.concat(chunks);
}

/** A terminal from `process.createPty`; its output goes to `onData`. */
export class PtyHandle {
  #exitCode: number | undefined;
  #error: string | undefined;
  readonly #done: Promise<void>;

  constructor(
    private readonly terminal: Terminal,
    readonly sessionId: string,
    private readonly process: Process,
    onData: ((data: Uint8Array) => void | Promise<void>) | undefined,
  ) {
    this.#done = (async () => {
      for await (const chunk of terminal.output()) await onData?.(chunk);
    })();
  }

  get exitCode(): number | undefined {
    return this.#exitCode;
  }

  get error(): string | undefined {
    return this.#error;
  }

  isConnected(): boolean {
    return this.#exitCode === undefined && this.#error === undefined;
  }

  /** The connection is open once the handle exists. */
  async waitForConnection(): Promise<void> {}

  async sendInput(data: string | Uint8Array): Promise<void> {
    this.terminal.send(data);
  }

  async resize(cols: number, rows: number): Promise<PtySessionInfo> {
    return this.process.resizePtySession(this.sessionId, cols, rows);
  }

  async kill(): Promise<void> {
    await this.process.killPtySession(this.sessionId);
  }

  async wait(): Promise<PtyResult> {
    await this.#done;
    try {
      this.#exitCode = (await this.terminal.wait()).exitCode;
    } catch (error) {
      if (!(error instanceof DaytonaError)) throw error;
      this.#error = error.message;
    }
    return { exitCode: this.#exitCode, error: this.#error };
  }

  async disconnect(): Promise<void> {
    this.terminal.detach();
  }
}

export interface PortPreviewUrl {
  url: string;
  token: string;
}

interface Settings {
  language: string;
  labels: Record<string, string>;
  public: boolean;
  /** The snapshot the sandbox started from. */
  snapshot?: string;
}

const response = (result: CommandResult): ExecuteResponse => ({ exitCode: result.exitCode, result: result.stdout });

export class Process {
  constructor(
    private readonly sandbox: CompatSandbox,
    private readonly language: string,
  ) {}

  /** Run `command`; `result` holds stdout and stderr together, as on Daytona. Timeouts are in seconds. */
  async executeCommand(command: string, cwd?: string, env?: Record<string, string>, timeout?: number): Promise<ExecuteResponse> {
    return response(await this.sandbox.run(`(\n${command}\n) 2>&1`, { cwd, envs: env, timeout }));
  }

  /** Run `code` with the sandbox's language. Charts are not captured. */
  async codeRun(code: string, params: CodeRunParams = {}, timeout?: number): Promise<ExecuteResponse> {
    const [extension, runner] = LANGUAGES[this.language]!;
    const path = `${CODE_DIR}/${randomUUID()}.${extension}`;
    await this.sandbox.write(path, code);
    const argv = (params.argv ?? []).map(shellQuote).join(" ");
    return this.executeCommand(`${runner} ${path} ${argv}; status=$?; rm -f ${path}; exit $status`, undefined, params.env, timeout);
  }

  /** A shell that keeps its directory and variables from one command to the next. */
  async createSession(sessionId: string): Promise<void> {
    await this.sandbox.createNamedSession(sessionId);
  }

  async #session(sessionId: string): Promise<string> {
    const session = (await this.sandbox.namedSessions()).get(sessionId);
    if (!session) throw new DaytonaNotFoundError(`Session ${sessionId} not found`);
    return session;
  }

  async #described(sessionId: string, arkerSession: string): Promise<Session> {
    const commands = await this.sandbox.sessionCommands(arkerSession);
    return { sessionId, commands: commands.map(([id, command, exitCode]) => ({ id, command, ...(exitCode === null ? {} : { exitCode }) })) };
  }

  async getSession(sessionId: string): Promise<Session> {
    return this.#described(sessionId, await this.#session(sessionId));
  }

  async listSessions(): Promise<Session[]> {
    const sessions: Session[] = [];
    for (const [name, arkerSession] of await this.sandbox.namedSessions()) sessions.push(await this.#described(name, arkerSession));
    return sessions;
  }

  async deleteSession(sessionId: string): Promise<void> {
    await this.sandbox.killSession(await this.#session(sessionId));
  }

  /**
   * Run a command in the session's shell. With `runAsync` it returns at once
   * and takes input from `sendSessionCommandInput`. `output` is stdout followed
   * by stderr; Arker keeps the two apart. Timeouts are in seconds.
   */
  async executeSessionCommand(sessionId: string, req: SessionExecuteRequest, timeout?: number): Promise<SessionExecuteResponse> {
    const arkerSession = await this.#session(sessionId);
    if (req.runAsync || req.async) return { cmdId: (await this.sandbox.startInSession(arkerSession, req.command)).runId };
    const [cmdId, result] = await this.sandbox.runInSession(arkerSession, req.command, timeout);
    return { cmdId, output: result.stdout + result.stderr, stdout: result.stdout, stderr: result.stderr, exitCode: result.exitCode };
  }

  async getSessionCommand(sessionId: string, commandId: string): Promise<Command> {
    const command = (await this.getSession(sessionId)).commands.find((command) => command.id === commandId);
    if (!command) throw new DaytonaNotFoundError(`Command ${commandId} not found in session ${sessionId}`);
    return command;
  }

  async getSessionCommandLogs(sessionId: string, commandId: string): Promise<SessionCommandLogsResponse>;
  async getSessionCommandLogs(
    sessionId: string,
    commandId: string,
    onStdout: (chunk: string) => void,
    onStderr: (chunk: string) => void,
  ): Promise<void>;
  /** The command's output; with callbacks, delivered as it arrives until the command ends. */
  async getSessionCommandLogs(
    sessionId: string,
    commandId: string,
    onStdout?: (chunk: string) => void,
    onStderr?: (chunk: string) => void,
  ): Promise<SessionCommandLogsResponse | void> {
    const process = this.sandbox.sessionProcess(await this.#session(sessionId), commandId);
    if (onStdout || onStderr) {
      for await (const [stdout, stderr] of process.follow()) {
        if (stdout) onStdout?.(stdout);
        if (stderr) onStderr?.(stderr);
      }
      return;
    }
    await process.poll();
    return { output: process.stdout + process.stderr, stdout: process.stdout, stderr: process.stderr };
  }

  /** Write `data` to the stdin of a command started with `runAsync`. */
  async sendSessionCommandInput(sessionId: string, commandId: string, data: string): Promise<void> {
    await this.sandbox.sessionProcess(await this.#session(sessionId), commandId).sendStdin(data);
  }

  async createPty(options: PtyCreateOptions & Partial<PtyConnectOptions>): Promise<PtyHandle> {
    const { id, cwd, envs, cols = 80, rows = 24, onData } = options;
    if ((await this.sandbox.processes()).some((info) => info.name === id)) throw new DaytonaError(`PTY session ${id} already exists.`);
    const terminal = await this.sandbox.openTerminal({ cols, rows, cwd, envs, name: id });
    return guardUnsupported(new PtyHandle(terminal, id, this, onData), ERRORS, "PtyHandle");
  }

  async connectPty(sessionId: string, options?: PtyConnectOptions): Promise<PtyHandle> {
    const terminal = await this.sandbox.attachTerminal(await this.#pty(sessionId));
    return guardUnsupported(new PtyHandle(terminal, sessionId, this, options?.onData), ERRORS, "PtyHandle");
  }

  async killPtySession(sessionId: string): Promise<void> {
    await this.sandbox.killSession((await this.#pty(sessionId)).sessionId);
  }

  async resizePtySession(sessionId: string, cols: number, rows: number): Promise<PtySessionInfo> {
    const info = await this.#pty(sessionId);
    await this.sandbox.resizeTerminal(info.sessionId, cols, rows);
    return { id: sessionId, cwd: info.cwd ?? "", envs: info.envs, cols, rows, active: true, lazyStart: false };
  }

  /** The terminals made with {@link createPty} that are still running. */
  async listPtySessions(): Promise<PtySessionInfo[]> {
    return (await this.sandbox.processes()).filter((info) => info.pty && info.name).map(ptyInfo);
  }

  async getPtySessionInfo(sessionId: string): Promise<PtySessionInfo> {
    return ptyInfo(await this.#pty(sessionId));
  }

  async #pty(sessionId: string): Promise<ProcessInfo> {
    const info = (await this.sandbox.processes()).find((info) => info.pty && info.name === sessionId);
    if (!info) throw new DaytonaNotFoundError(`PTY session ${sessionId} not found`);
    return info;
  }
}

export class FileSystem {
  constructor(private readonly sandbox: CompatSandbox) {}

  /** Upload `file`: its bytes, or the path of a local file. */
  async uploadFile(file: Uint8Array | string, remotePath: string, _timeout?: number): Promise<void> {
    await this.sandbox.write(remotePath, await readSource(file));
  }

  async uploadFiles(files: FileUpload[], _timeout?: number): Promise<void> {
    for (const file of files) await this.uploadFile(file.source, file.destination);
  }

  /** Upload bytes, a local file path or a stream; the whole file is sent at once. */
  async uploadFileStream(source: UploadSource, remotePath: string, options?: { timeout?: number; [option: string]: unknown }): Promise<void> {
    const { timeout: _timeout, ...rest } = options ?? {};
    rejectOptions(ERRORS, "fs.uploadFileStream", rest);
    await this.sandbox.write(remotePath, await readSource(source));
  }

  async downloadFile(remotePath: string, _timeout?: number): Promise<Buffer>;
  async downloadFile(remotePath: string, localPath: string, _timeout?: number): Promise<void>;
  async downloadFile(remotePath: string, localPath?: string | number): Promise<Buffer | void> {
    const data = Buffer.from(await this.sandbox.read(remotePath));
    if (typeof localPath !== "string") return data;
    await writeFile(localPath, data);
  }

  /** Download each file; a failure is reported in its response, not thrown. */
  async downloadFiles(files: FileDownloadRequest[], _timeoutSec?: number): Promise<FileDownloadResponse[]> {
    const responses: FileDownloadResponse[] = [];
    for (const { source, destination } of files) {
      try {
        const data = Buffer.from(await this.sandbox.read(source));
        if (destination) await writeFile(destination, data);
        responses.push({ source, result: destination ?? data });
      } catch (error) {
        if (!(error instanceof DaytonaError)) throw error;
        responses.push({ source, error: error.message });
      }
    }
    return responses;
  }

  /** The file's content, fetched whole and streamed as one chunk. */
  async downloadFileStream(remotePath: string, options?: number | { timeout?: number; [option: string]: unknown }): Promise<Readable> {
    const { timeout: _timeout, ...rest } = typeof options === "object" ? options : {};
    rejectOptions(ERRORS, "fs.downloadFileStream", rest);
    return Readable.from([Buffer.from(await this.sandbox.read(remotePath))]);
  }

  async listFiles(path: string, options: { depth?: number } = {}): Promise<FileInfo[]> {
    return (await this.sandbox.list(path, options.depth ?? 1)).map(fileInfo);
  }

  async getFileDetails(path: string): Promise<FileInfo> {
    return fileInfo(await this.sandbox.stat(path));
  }

  async createFolder(path: string, mode = "755"): Promise<void> {
    await this.sandbox.makeDir(path);
    await this.setFilePermissions(path, { mode });
  }

  async setFilePermissions(path: string, permissions: FilePermissionsParams): Promise<void> {
    const { mode, owner, group } = permissions;
    const steps = mode ? [`chmod ${shellQuote(mode)} ${shellQuote(path)}`] : [];
    if (owner || group) steps.push(`chown ${shellQuote((owner ?? "") + (group ? `:${group}` : ""))} ${shellQuote(path)}`);
    if (steps.length > 0) await this.#shell(steps.join(" && "));
  }

  /** Lines matching `pattern`, a grep regular expression, in `path` and below. */
  async findFiles(path: string, pattern: string): Promise<Match[]> {
    const result = await this.sandbox.run(`grep -rnIZ -e ${shellQuote(pattern)} -- ${shellQuote(path)}`);
    if (result.exitCode > 1) throw new DaytonaError(result.stderr.trim() || `grep failed with exit ${result.exitCode}`);
    return result.stdout
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const [file, rest = ""] = line.split("\0", 2) as [string, string];
        const colon = rest.indexOf(":");
        return { file, line: Number(rest.slice(0, colon)), content: rest.slice(colon + 1) };
      });
  }

  /** Paths in and below `path` whose name matches the glob `pattern`. */
  async searchFiles(path: string, pattern: string): Promise<SearchFilesResponse> {
    const found = await this.#shell(`find ${shellQuote(path)} -mindepth 1 -name ${shellQuote(pattern)} -print0`);
    return { files: found.split("\0").filter(Boolean) };
  }

  /** Replace every occurrence of the text `pattern` in each file. */
  async replaceInFiles(files: string[], pattern: string, newValue: string): Promise<ReplaceResult[]> {
    const results: ReplaceResult[] = [];
    for (const file of files) {
      try {
        const text = new TextDecoder("utf-8", { fatal: true }).decode(await this.sandbox.read(file));
        await this.sandbox.write(file, text.split(pattern).join(newValue));
        results.push({ file, success: true });
      } catch (error) {
        if (!(error instanceof DaytonaError || error instanceof TypeError)) throw error;
        results.push({ file, success: false, error: error.message });
      }
    }
    return results;
  }

  async deleteFile(path: string, recursive = false): Promise<void> {
    await this.sandbox.remove(path, { recursive });
  }

  async moveFiles(source: string, destination: string): Promise<void> {
    await this.sandbox.rename(source, destination);
  }

  async #shell(script: string): Promise<string> {
    const result = await this.sandbox.run(script);
    if (result.exitCode !== 0) throw new DaytonaError(result.stderr.trim() || `exit ${result.exitCode}`);
    return result.stdout;
  }
}

// Daytona's name for each porcelain status letter.
const GIT_FILE_STATES: Record<string, string> = {
  " ": "Unmodified",
  "?": "Untracked",
  M: "Modified",
  A: "Added",
  D: "Deleted",
  R: "Renamed",
  C: "Copied",
  U: "Updated but unmerged",
};

export interface FileStatus {
  name: string;
  staging: string;
  worktree: string;
  extra: string;
}

export interface GitStatus {
  currentBranch: string;
  fileStatus: FileStatus[];
  ahead?: number;
  behind?: number;
  branchPublished?: boolean;
  detached?: boolean;
  upstream?: string;
}

export interface ListBranchResponse {
  branches: string[];
  current?: string;
}

export interface ListRemotesResponse {
  remotes: { name: string; url: string }[];
}

export interface GitCommitResponse {
  sha: string;
}

const state = (letter: string): string => GIT_FILE_STATES[letter] ?? "Unmodified";
const credentials = (username?: string, password?: string) => (username || password ? { username, password } : {});

/** Git in the sandbox, run as the git CLI. Relative paths are under the working directory. */
export class Git {
  readonly #git: CompatGit;

  constructor(sandbox: CompatSandbox) {
    this.#git = new CompatGit(sandbox);
  }

  async clone(
    url: string,
    path: string,
    branch?: string,
    commitId?: string,
    username?: string,
    password?: string,
    insecureSkipTls?: boolean,
    depth?: number,
  ): Promise<void> {
    await this.#git.clone(url, path, { branch, depth, commit: commitId, insecureSkipTls, ...credentials(username, password) });
  }

  async init(path: string, bare?: boolean, initialBranch?: string): Promise<void> {
    await this.#git.init(path, { bare, initialBranch });
  }

  async status(path: string): Promise<GitStatus> {
    const git = await this.#git.status(path);
    return {
      currentBranch: git.currentBranch ?? "",
      fileStatus: git.files.map((file) => ({
        name: file.name,
        staging: state(file.indexStatus),
        worktree: state(file.worktreeStatus),
        extra: file.renamedFrom ?? "",
      })),
      ahead: git.ahead,
      behind: git.behind,
      branchPublished: git.upstream !== undefined,
      detached: git.detached,
      ...(git.upstream ? { upstream: git.upstream } : {}),
    };
  }

  async branches(path: string): Promise<ListBranchResponse> {
    return this.#git.branches(path);
  }

  async createBranch(path: string, name: string): Promise<void> {
    await this.#git.createBranch(path, name);
  }

  async deleteBranch(path: string, name: string): Promise<void> {
    await this.#git.deleteBranch(path, name);
  }

  async checkoutBranch(path: string, branch: string): Promise<void> {
    await this.#git.checkoutBranch(path, branch);
  }

  async add(path: string, files: string[]): Promise<void> {
    await this.#git.add(path, files);
  }

  async commit(path: string, message: string, author: string, email: string, allowEmpty?: boolean): Promise<GitCommitResponse> {
    await this.#git.commit(path, message, { authorName: author, authorEmail: email, allowEmpty });
    return { sha: (await this.#git.run(["rev-parse", "HEAD"], path)).stdout.trim() };
  }

  async push(path: string, username?: string, password?: string, branch?: string, remote?: string, setUpstream?: boolean): Promise<void> {
    await this.#git.push(path, { remote, branch, setUpstream, ...credentials(username, password) });
  }

  async pull(path: string, username?: string, password?: string, branch?: string, remote?: string): Promise<void> {
    await this.#git.pull(path, { remote, branch, ...credentials(username, password) });
  }

  async reset(path: string, mode?: string, target?: string, files?: string[]): Promise<void> {
    await this.#git.reset(path, { mode, target, paths: files });
  }

  async restore(path: string, files: string[], staged?: boolean, worktree?: boolean, source?: string): Promise<void> {
    await this.#git.restore(path, files, { staged, worktree, source });
  }

  async remoteAdd(path: string, name: string, url: string, fetch?: boolean, overwrite?: boolean): Promise<void> {
    await this.#git.remoteAdd(path, name, url, { fetch, overwrite });
  }

  async remotes(path: string): Promise<ListRemotesResponse> {
    return { remotes: (await this.#git.remotes(path)).map(([name, url]) => ({ name, url })) };
  }

  async remoteGet(path: string, name: string): Promise<string | undefined> {
    return this.#git.remoteGet(path, name);
  }

  async setConfig(key: string, value: string, scope?: string, path?: string): Promise<void> {
    await this.#git.setConfig(key, value, { scope, path });
  }

  async getConfig(key: string, scope?: string, path?: string): Promise<string | undefined> {
    return this.#git.getConfig(key, { scope, path });
  }

  async configureUser(name: string, email: string, scope?: string, path?: string): Promise<void> {
    await this.#git.configureUser(name, email, { scope, path });
  }

  async dangerouslyAuthenticate(username: string, password: string, host?: string, protocol?: string): Promise<void> {
    await this.#git.dangerouslyAuthenticate(username, password, { host, protocol });
  }
}

// Daytona's code interpreter runs for at most this long unless told otherwise.
const DEFAULT_CODE_TIMEOUT = 600;

export interface OutputMessage {
  output: string;
}

export interface ExecutionError {
  name: string;
  value: string;
  traceback?: string;
}

export interface ExecutionResult {
  stdout: string;
  stderr: string;
  error?: ExecutionError;
}

export interface InterpreterContext {
  id: string;
  cwd: string;
  language: string;
  active: boolean;
  createdAt: string;
}

export interface RunCodeOptions {
  context?: InterpreterContext;
  envs?: Record<string, string>;
  /** Seconds; 0 for none. Default 10 minutes. */
  timeout?: number;
  onStdout?: (message: OutputMessage) => any | Promise<any>;
  onStderr?: (message: OutputMessage) => any | Promise<any>;
  onError?: (error: ExecutionError) => any | Promise<any>;
}

/** A stateful Python interpreter: variables and imports carry over between runs in one context. Output reaches the callbacks once a run finishes. */
export class CodeInterpreter {
  readonly #contexts: CodeContexts;

  constructor(sandbox: CompatSandbox) {
    this.#contexts = new CodeContexts(sandbox);
  }

  async runCode(code: string, options: RunCodeOptions = {}): Promise<ExecutionResult> {
    const { context, envs, timeout, onStdout, onStderr, onError } = options;
    const done = await this.#contexts.run(code, { contextId: context?.id, envs, timeout: timeout === 0 ? undefined : (timeout ?? DEFAULT_CODE_TIMEOUT) });
    if (done.stdout) await onStdout?.({ output: done.stdout });
    if (done.stderr) await onStderr?.({ output: done.stderr });
    if (done.error) await onError?.(done.error);
    return { stdout: done.stdout, stderr: done.stderr, ...(done.error ? { error: done.error } : {}) };
  }

  async createContext(cwd?: string): Promise<InterpreterContext> {
    return interpreterContext(await this.#contexts.create(cwd));
  }

  /** The contexts made with {@link CodeInterpreter.createContext}; the default context is not listed. */
  async listContexts(): Promise<InterpreterContext[]> {
    return (await this.#contexts.list()).filter((context) => !context.default).map(interpreterContext);
  }

  async deleteContext(context: InterpreterContext): Promise<void> {
    await this.#contexts.delete(context.id);
  }
}

function interpreterContext(context: CodeContext): InterpreterContext {
  return { id: context.id, cwd: context.cwd, language: "python", active: true, createdAt: "" };
}

export interface VolumeMount {
  /** The id or name of the volume. */
  volumeId: string;
  mountPath: string;
  subpath?: string;
}

/** A shared volume, as an Arker filesystem. */
export interface Volume {
  id: string;
  name: string;
  organizationId: string;
  state: string;
  createdAt: string;
}

const volume = (filesystem: Filesystem): Volume => ({
  id: filesystem.filesystem_id,
  name: filesystem.name,
  organizationId: filesystem.owner_org_id,
  state: "ready",
  createdAt: filesystem.created_at,
});

export class VolumeService {
  readonly #volumes: Volumes;

  constructor(arker?: CompatConfig) {
    this.#volumes = new Volumes(ERRORS, arker);
  }

  async list(): Promise<Volume[]> {
    return (await this.#volumes.list()).map(volume);
  }

  async get(name: string, create = false): Promise<Volume> {
    return volume(await this.#volumes.get(name, { create }));
  }

  async create(name: string): Promise<Volume> {
    return volume(await this.#volumes.create(name));
  }

  async delete(target: Volume): Promise<void> {
    if (!(await this.#volumes.delete(target.id))) throw new DaytonaNotFoundError(`Volume ${target.id} not found`);
  }

  /** @internal Mount path to volume id, for mounts naming a volume by id or by name. */
  async mounts(mounts: VolumeMount[] | undefined): Promise<Record<string, string> | undefined> {
    if (!mounts?.length) return undefined;
    const resolved: Record<string, string> = {};
    for (const mount of mounts) {
      rejectOptions(ERRORS, "VolumeMount", { subpath: mount.subpath });
      const found = (await this.#volumes.find(mount.volumeId)) ?? (await this.#volumes.byId(mount.volumeId));
      resolved[mount.mountPath] = found.filesystem_id;
    }
    return resolved;
  }
}

/**
 * A Daytona sandbox running as an Arker VM. Arker suspends a VM when it is
 * idle and resumes it when used, so stopping, pausing and archiving all
 * suspend it, and starting or recovering it needs nothing.
 */
export class Sandbox {
  readonly id: string;
  readonly name: string | undefined;
  labels: Record<string, string>;
  public: boolean;
  networkBlockAll: boolean;
  /** Read with refreshData(); listing does not fetch it. */
  env: Record<string, string> | undefined;
  state: "started" | "stopped" | "paused" | "archived" = "started";
  readonly process: Process;
  readonly fs: FileSystem;
  readonly git: Git;
  readonly codeInterpreter: CodeInterpreter;

  /** @internal */
  constructor(
    private readonly sandbox: CompatSandbox,
    settings: Partial<Settings>,
    env?: Record<string, string>,
  ) {
    this.id = sandbox.id;
    this.name = sandbox.vm.name ?? undefined;
    this.labels = settings.labels ?? {};
    this.public = settings.public ?? false;
    this.networkBlockAll = blocksAllEgress(sandbox.vm);
    this.env = env;
    this.process = guardUnsupported(new Process(sandbox, settings.language ?? "python"), ERRORS, "sandbox.process");
    this.fs = guardUnsupported(new FileSystem(sandbox), ERRORS, "sandbox.fs");
    this.git = guardUnsupported(new Git(sandbox), ERRORS, "sandbox.git");
    this.codeInterpreter = guardUnsupported(new CodeInterpreter(sandbox), ERRORS, "sandbox.codeInterpreter");
  }

  async start(_timeout?: number): Promise<void> {
    this.state = "started";
  }

  async recover(_timeout?: number): Promise<void> {
    this.state = "started";
  }

  async stop(_timeout?: number, _force?: boolean): Promise<void> {
    await this.sandbox.suspend();
    this.state = "stopped";
  }

  async pause(_timeout?: number): Promise<void> {
    await this.sandbox.suspend();
    this.state = "paused";
  }

  async archive(): Promise<void> {
    await this.sandbox.suspend();
    this.state = "archived";
  }

  async waitUntilStarted(_timeout?: number): Promise<void> {}

  async waitUntilStopped(_timeout?: number): Promise<void> {}

  async refreshData(): Promise<void> {
    const settings = await this.sandbox.metadata();
    this.labels = settings.labels ?? {};
    this.public = settings.public ?? false;
    this.networkBlockAll = blocksAllEgress(await this.sandbox.info());
    this.env = await this.sandbox.env();
  }

  /** Nothing stops an idle Arker sandbox for good, so there is no timer to reset. */
  async refreshActivity(): Promise<void> {}

  async setLabels(labels: Record<string, string>): Promise<Record<string, string>> {
    this.labels = (await this.sandbox.updateMetadata({ labels })).labels;
    return this.labels;
  }

  /** Make the ports already open, and those opened later, public or private. */
  async setPublic(isPublic: boolean): Promise<void> {
    await this.sandbox.updateMetadata({ public: isPublic });
    await this.sandbox.setPortsPublic(isPublic);
    this.public = isPublic;
  }

  async updateNetworkSettings(settings: UpdateSandboxNetworkSettings): Promise<void> {
    const { networkBlockAll, networkAllowList, domainAllowList } = settings;
    if (networkBlockAll === undefined && networkAllowList === undefined && domainAllowList === undefined) {
      throw new DaytonaValidationError("At least one of networkBlockAll, networkAllowList or domainAllowList must be set");
    }
    const egress = egressFor(networkBlockAll, networkAllowList, domainAllowList);
    await this.sandbox.updateEgress(egress);
    this.networkBlockAll = egress?.denyAll === true && !egress.allowIps?.length && !egress.allowHosts?.length;
  }

  /** Nothing to wait for: {@link resize} returns once the sandbox is resized. */
  async waitForResizeComplete(_timeout?: number): Promise<void> {}

  /** Delete the sandbox `ttlMinutes` from now, whatever its state; 0 cancels it. */
  async setTtl(ttlMinutes: number): Promise<void> {
    if (ttlMinutes < 0) throw new DaytonaValidationError("TTL must be a non-negative integer");
    if (ttlMinutes) await this.sandbox.setTimeout(ttlMinutes * 60);
    else await this.sandbox.clearTimeout();
  }

  /**
   * Arker suspends an idle sandbox and resumes it when used, by itself, and does not track how
   * long one has been stopped, so these timers do nothing.
   */
  #idleInterval(method: string, interval: number, allowNegative = false): void {
    if (interval < 0 && !allowNegative) throw new DaytonaValidationError(`The ${method} interval must be a non-negative integer`);
    rejectOptions(ERRORS, `Sandbox.${method}`, { interval }, new Set(), new Set(["interval"]));
  }

  async setAutostopInterval(interval: number): Promise<void> {
    this.#idleInterval("setAutostopInterval", interval);
  }

  async setAutoPauseInterval(interval: number): Promise<void> {
    this.#idleInterval("setAutoPauseInterval", interval);
  }

  async setAutoArchiveInterval(interval: number): Promise<void> {
    this.#idleInterval("setAutoArchiveInterval", interval);
  }

  /** Ignored with a warning; {@link setTtl} deletes the sandbox at a set time. */
  async setAutoDeleteInterval(interval: number): Promise<void> {
    this.#idleInterval("setAutoDeleteInterval", interval, true);
  }

  /** A URL that downloads `path` with no credentials, for `ttlSeconds` (default an hour; zero or less, until {@link rotateSigningKey}). */
  async downloadUrl(path: string, ttlSeconds?: number): Promise<string> {
    return this.sandbox.fileUrl(path, { write: false, expiresIn: fileUrlTtl(ttlSeconds) });
  }

  /** A URL that writes a POSTed multipart/form-data file to `path` with no credentials. */
  async uploadUrl(path: string, ttlSeconds?: number): Promise<string> {
    return this.sandbox.fileUrl(path, { write: true, expiresIn: fileUrlTtl(ttlSeconds) });
  }

  /** Make every URL from {@link downloadUrl} and {@link uploadUrl} stop working. */
  async rotateSigningKey(): Promise<void> {
    await this.sandbox.revokeFileUrls();
  }

  /**
   * A URL to `port` that needs no other credential, for `expiresInSeconds` (default 60). The
   * token is the URL's `arker_token` query parameter; a browser that opens the URL keeps it in
   * a cookie for later requests.
   */
  async getSignedPreviewUrl(port: number, expiresInSeconds?: number): Promise<SignedPortPreviewUrl> {
    const { url, token } = await this.sandbox.signedPortUrl(port, expiresInSeconds || DEFAULT_PREVIEW_URL_TTL);
    return { sandboxId: this.id, port, token, url };
  }

  async expireSignedPreviewUrl(port: number, token: string): Promise<void> {
    await this.sandbox.revokePortUrl(port, token);
  }

  /** Change CPUs, and memory and disk in GiB. */
  async resize(resources: Resources, _timeout?: number): Promise<void> {
    rejectOptions(ERRORS, "Sandbox.resize", { gpu: resources.gpu });
    await this.sandbox.resize({
      cpus: resources.cpu,
      memoryMiB: resources.memory ? resources.memory * 1024 : undefined,
      diskMiB: resources.disk ? resources.disk * 1024 : undefined,
    });
  }

  /** Change the environment of commands started from now on. */
  async updateEnv(env: Record<string, string>, options: { unset?: string[] } = {}): Promise<void> {
    await this.sandbox.updateEnv(env, options.unset);
    this.env = undefined;
  }

  async getUserHomeDir(): Promise<string> {
    return this.sandbox.homeDir();
  }

  async getUserRootDir(): Promise<string> {
    return this.getUserHomeDir();
  }

  async getWorkDir(): Promise<string> {
    return this.sandbox.workDir();
  }

  /** Snapshot the sandbox; create sandboxes from it with `{ snapshot: name }`. */
  async createSnapshot(name: string, _timeout?: number): Promise<void> {
    await this.sandbox.snapshot(name);
  }

  /** A new sandbox from this one's files and memory, with its settings. */
  async fork(params: ForkSandboxParams = {}, _timeout?: number): Promise<Sandbox> {
    const forked = await this.sandbox.fork({ name: params.name });
    return guardUnsupported(new Sandbox(forked, forked.settings, this.env), ERRORS, "Sandbox");
  }

  /**
   * Open `port`. A non-public sandbox's port takes
   * `Authorization: Bearer $ARKER_API_KEY` in place of a preview token.
   */
  async getPreviewLink(port: number): Promise<PortPreviewUrl> {
    return { url: `https://${await this.sandbox.expose(port, { public: this.public })}`, token: "" };
  }

  async delete(_timeout?: number, _wait?: boolean): Promise<void> {
    await this.sandbox.delete();
  }

  /** The Arker VM behind this sandbox, for anything this layer does not cover. */
  get arker(): VM {
    return this.sandbox.vm;
  }
}

function resource(sandbox: CompatSandbox, name: "vcpu" | "memory_mib" | "disk_mib", divisor = 1): number {
  return ((sandbox.vm as VM & Vm).resources?.[name] ?? 0) / divisor;
}

const compare = (a: string | number, b: string | number): number => (a < b ? -1 : a > b ? 1 : 0);

const SORT_KEYS: Record<string, (sandbox: CompatSandbox) => string | number> = {
  name: (sandbox) => sandbox.vm.name ?? "",
  cpu: (sandbox) => resource(sandbox, "vcpu"),
  memoryGib: (sandbox) => resource(sandbox, "memory_mib", 1024),
  diskGib: (sandbox) => resource(sandbox, "disk_mib", 1024),
  lastActivityAt: (sandbox) => (sandbox.vm as VM & Vm).last_active_at ?? "",
  createdAt: (sandbox) => `${(sandbox.vm as VM & Vm).created_at} ${sandbox.id.split("-").pop()}`,
};

/** A test for the filters `Daytona.list` takes, which it removes from `filters`. */
function listFilter(filters: Record<string, unknown>): (sandbox: CompatSandbox) => boolean {
  const take = <T>(name: string): T | undefined => {
    const value = filters[name] as T | undefined;
    delete filters[name];
    return value;
  };
  const tests: Array<(sandbox: CompatSandbox) => boolean> = [];
  const id = (take<string>("id") ?? "").toLowerCase();
  const name = (take<string>("name") ?? "").toLowerCase();
  const labels = take<Record<string, string>>("labels") ?? {};
  tests.push((sandbox) => sandbox.id.toLowerCase().startsWith(id));
  tests.push((sandbox) => (sandbox.vm.name ?? "").toLowerCase().startsWith(name));
  tests.push((sandbox) => Object.entries(labels).every(([key, value]) => (sandbox.settings.labels ?? {})[key] === value));
  const states = take<string[]>("states");
  if (states !== undefined) tests.push(() => states.includes("started"));
  const snapshots = take<string[]>("snapshots");
  if (snapshots !== undefined) tests.push((sandbox) => snapshots.includes(sandbox.settings.snapshot));
  const isPublic = take<boolean>("isPublic");
  if (isPublic !== undefined) tests.push((sandbox) => Boolean(sandbox.settings.public) === isPublic);
  const measures = { Cpu: SORT_KEYS.cpu!, MemoryGib: SORT_KEYS.memoryGib!, DiskGib: SORT_KEYS.diskGib! };
  for (const [suffix, measure] of Object.entries(measures)) {
    const low = take<number>(`min${suffix}`);
    const high = take<number>(`max${suffix}`);
    if (low !== undefined) tests.push((sandbox) => (measure(sandbox) as number) >= low);
    if (high !== undefined) tests.push((sandbox) => (measure(sandbox) as number) <= high);
  }
  const times = { createdAt: "created_at", lastActivity: "last_active_at" } as const;
  for (const [prefix, field] of Object.entries(times)) {
    const after = take<Date>(`${prefix}After`);
    const before = take<Date>(`${prefix}Before`);
    const moment = (sandbox: CompatSandbox) => {
      const text = (sandbox.vm as VM & Vm)[field];
      return text ? Date.parse(text) : undefined;
    };
    if (after !== undefined) tests.push((sandbox) => (moment(sandbox) ?? -Infinity) > after.getTime());
    if (before !== undefined) tests.push((sandbox) => (moment(sandbox) ?? Infinity) < before.getTime());
  }
  return (sandbox) => tests.every((test) => test(sandbox));
}

/** Seconds a signed file URL works, or undefined for no expiry, as Daytona reads `ttlSeconds`. */
function fileUrlTtl(ttlSeconds: number | undefined): number | undefined {
  if (ttlSeconds === undefined) return DEFAULT_FILE_URL_TTL;
  return ttlSeconds > 0 ? ttlSeconds : undefined;
}

function snapshotOf(compat: CompatSandbox): Snapshot {
  const vm = compat.vm as VM & Vm;
  const resources = vm.resources;
  return {
    id: vm.id,
    name: vm.name ?? vm.id,
    imageName: compat.settings.image ?? vm.root_source_vm_name ?? undefined,
    state: "active",
    cpu: resources?.vcpu ?? undefined,
    gpu: 0,
    mem: resources?.memory_mib ? Math.floor(resources.memory_mib / 1024) : undefined,
    disk: resources?.disk_mib ? Math.floor(resources.disk_mib / 1024) : undefined,
    entrypoint: null,
    general: false,
    errorReason: null,
    createdAt: new Date(vm.created_at ?? Date.now()),
    sourceSandboxId: compat.snapshotOf !== SNAPSHOT_OF_IMAGE ? (compat.snapshotOf ?? null) : null,
  };
}

/**
 * Snapshots: those of sandboxes ({@link Sandbox.createSnapshot}), and those made here from
 * registry images. Sandboxes start from one with `{ snapshot: name }`.
 */
export class SnapshotService {
  constructor(private readonly arker?: CompatConfig) {}

  /** A snapshot of a registry image, ready as soon as this returns. */
  async create(params: CreateSnapshotParams, options: { onLogs?: (chunk: string) => void; timeout?: number } = {}): Promise<Snapshot> {
    const { name, image, resources = {}, regionId: _regionId, ...rest } = params;
    if (typeof image !== "string") throw new DaytonaValidationError("Only image names are supported on Arker, not Image builders.");
    rejectOptions(ERRORS, "Resources", { gpu: resources.gpu, gpuType: (resources as { gpuType?: unknown }).gpuType });
    rejectOptions(ERRORS, "CreateSnapshotParams", rest, new Set(), new Set(["sandboxClass"]));
    options.onLogs?.(`Creating snapshot ${name} from ${image}`);
    const compat = await CompatSandbox.create(ERRORS, {
      config: this.arker,
      image,
      cpus: resources.cpu,
      memoryMiB: resources.memory ? resources.memory * 1024 : undefined,
      diskMiB: resources.disk ? resources.disk * 1024 : undefined,
      metadata: { image },
      name,
      snapshotOf: SNAPSHOT_OF_IMAGE,
    });
    options.onLogs?.(`Created snapshot ${name} (active)`);
    return snapshotOf(compat);
  }

  /** Snapshots, oldest first, `limit` (default 100) to a page. */
  async list(query?: { page?: number; limit?: number; sourceSandboxId?: string }): Promise<PaginatedSnapshots>;
  async list(page?: number, limit?: number): Promise<PaginatedSnapshots>;
  async list(queryOrPage?: { page?: number; limit?: number; sourceSandboxId?: string } | number, maybeLimit?: number): Promise<PaginatedSnapshots> {
    const query = typeof queryOrPage === "object" ? queryOrPage : { page: queryOrPage, limit: maybeLimit };
    if (query.page !== undefined && query.page < 1) throw new DaytonaValidationError("page must be a positive integer");
    if (query.limit !== undefined && query.limit < 1) throw new DaytonaValidationError("limit must be a positive integer");
    const page = query.page ?? 1;
    const limit = query.limit ?? 100;
    const found: Snapshot[] = [];
    for await (const compat of CompatSandbox.find(ERRORS, this.arker, { snapshots: true })) {
      const snapshot = snapshotOf(compat);
      if (query.sourceSandboxId === undefined || snapshot.sourceSandboxId === query.sourceSandboxId) found.push(snapshot);
    }
    const key = (snapshot: Snapshot) => `${snapshot.createdAt.toISOString()} ${snapshot.id.split("-").pop()}`;
    found.sort((a, b) => key(a).localeCompare(key(b)));
    const items = found.slice((page - 1) * limit, page * limit);
    return { items, total: found.length, page, totalPages: Math.max(1, Math.ceil(found.length / limit)) };
  }

  async #find(snapshot: Snapshot | string): Promise<CompatSandbox> {
    const name = typeof snapshot === "string" ? snapshot : snapshot.id;
    const compat = await CompatSandbox.connect(ERRORS, name, this.arker);
    if (!compat || compat.snapshotOf === undefined) throw new DaytonaNotFoundError(`Snapshot ${name} not found`);
    return compat;
  }

  /** A snapshot by name or id. */
  async get(idOrName: string): Promise<Snapshot> {
    return snapshotOf(await this.#find(idOrName));
  }

  async delete(snapshot: Snapshot | string): Promise<void> {
    await (await this.#find(snapshot)).delete();
  }

  /** Snapshots never go inactive on Arker, so this only looks it up. */
  async activate(snapshot: Snapshot | string): Promise<Snapshot> {
    return snapshotOf(await this.#find(snapshot));
  }
}

export class Daytona {
  readonly volume: VolumeService;
  readonly snapshot: SnapshotService;

  constructor(private readonly config: DaytonaConfig = {}) {
    this.volume = guardUnsupported(new VolumeService(config.arker), ERRORS, "daytona.volume");
    this.snapshot = guardUnsupported(new SnapshotService(config.arker), ERRORS, "daytona.snapshot");
    return guardUnsupported(this, ERRORS, "Daytona");
  }

  async create(params: CreateSandboxFromSnapshotParams | CreateSandboxFromImageParams = {}, _options?: object): Promise<Sandbox> {
    const { language = "python", image, snapshot, envVars, labels = {}, public: isPublic = false, resources = {}, networkBlockAll, networkAllowList, name, volumes, ...rest } =
      params as CreateSandboxBaseParams & { image?: unknown; snapshot?: string };
    if (!(language in LANGUAGES)) {
      throw new DaytonaValidationError(`Unsupported language '${language}'; use one of ${Object.keys(LANGUAGES).join(", ")}.`);
    }
    if (image !== undefined && typeof image !== "string") {
      throw new DaytonaValidationError("Only image names are supported on Arker, not Image builders.");
    }
    rejectOptions(ERRORS, "Resources", { gpu: resources.gpu });
    rejectOptions(ERRORS, "Daytona.create", rest, new Set(), INERT_PARAMS);
    const mounts = await this.volume.mounts(volumes);
    const settings: Settings = { language, labels, public: isPublic, ...(snapshot ? { snapshot } : {}) };
    const sandbox = await CompatSandbox.create(ERRORS, {
      config: this.config.arker,
      source: snapshot,
      image,
      envs: envVars,
      cpus: resources.cpu,
      memoryMiB: resources.memory ? resources.memory * 1024 : undefined,
      diskMiB: resources.disk ? resources.disk * 1024 : undefined,
      egress: egressFor(networkBlockAll, networkAllowList),
      metadata: settings,
      name,
      volumes: mounts,
    });
    return guardUnsupported(new Sandbox(sandbox, settings, envVars ?? {}), ERRORS, "Sandbox");
  }

  /**
   * An id or name Arker does not know is handed to the `@daytonaio/sdk`
   * package when it is installed, so existing Daytona sandboxes keep working.
   */
  async get(sandboxIdOrName: string): Promise<Sandbox> {
    const sandbox = await CompatSandbox.connect(ERRORS, sandboxIdOrName, this.config.arker);
    if (!sandbox) return (await this.#native(sandboxIdOrName)).get(sandboxIdOrName);
    return guardUnsupported(new Sandbox(sandbox, await sandbox.metadata(), await sandbox.env()), ERRORS, "Sandbox");
  }

  /** The sandboxes this layer created that match `query` (see {@link ListSandboxesQuery}). */
  async *list(query: ListSandboxesQuery = {}): AsyncIterableIterator<Sandbox> {
    const { sort, order, limit: _limit, ...filters } = query;
    const matches = listFilter(filters);
    rejectOptions(ERRORS, "ListSandboxesQuery", filters);
    const sortKey = sort === undefined ? undefined : SORT_KEYS[sort];
    if (sort !== undefined && !sortKey) {
      throw new DaytonaValidationError(`Cannot sort by '${sort}'; use one of ${Object.keys(SORT_KEYS).join(", ")}.`);
    }
    const found: CompatSandbox[] = [];
    for await (const sandbox of CompatSandbox.find(ERRORS, this.config.arker)) {
      if (!matches(sandbox)) continue;
      if (!sortKey) yield guardUnsupported(new Sandbox(sandbox, sandbox.settings), ERRORS, "Sandbox");
      else found.push(sandbox);
    }
    const direction = order === "desc" ? -1 : 1;
    found.sort((a, b) => compare(sortKey!(a), sortKey!(b)) * direction);
    for (const sandbox of found) yield guardUnsupported(new Sandbox(sandbox, sandbox.settings), ERRORS, "Sandbox");
  }

  async start(sandbox: Sandbox, timeout?: number): Promise<void> {
    await sandbox.start(timeout);
  }

  async stop(sandbox: Sandbox): Promise<void> {
    await sandbox.stop();
  }

  async fork(sandbox: Sandbox, params?: ForkSandboxParams, timeout?: number): Promise<Sandbox> {
    return sandbox.fork(params, timeout);
  }

  async delete(sandbox: Sandbox | { id: string } | string, _timeout?: number): Promise<void> {
    if (sandbox instanceof Sandbox) return sandbox.delete();
    await (await this.get(typeof sandbox === "string" ? sandbox : sandbox.id)).delete();
  }

  async #native(sandboxId: string): Promise<{ get(id: string): Promise<Sandbox> }> {
    const native = await importOptional<{ Daytona: new (config?: object) => { get(id: string): Promise<Sandbox> } }>("@daytonaio/sdk");
    if (!native) throw new DaytonaNotFoundError(`Sandbox ${sandboxId} not found`);
    const { arker: _arker, ...credentials } = this.config;
    return new native.Daytona(Object.keys(credentials).length > 0 ? credentials : undefined);
  }
}
