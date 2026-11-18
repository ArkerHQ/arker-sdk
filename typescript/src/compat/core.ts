/**
 * The single path from every sandbox compatibility layer to Arker.
 *
 * The e2b, modal and daytona entry points only translate their provider's API
 * into {@link CompatSandbox} calls and its results back into the provider's
 * types. Every Arker request they make goes through here, so a fix here fixes
 * all of them.
 */
import { createHash, randomBytes } from "node:crypto";
import { Arker, ArkerError, VM, type ArkerOptions, type Filesystem, type PtyConnection, type Vm } from "../index.js";
import { FILE_SERVER, INTERPRETER, NODE_INTERPRETER } from "./interpreter.js";

/** Where and how to reach Arker, and which VM new sandboxes fork. */
export interface CompatConfig extends ArkerOptions {
  source?: string;
  name?: string;
}

// Public base VM closest to the images other sandbox providers default to:
// Python with pip, Node with npm, git and curl.
export const DEFAULT_SOURCE = "ubuntu-coding";
// Sandbox-level envs live in the VM, so they survive reconnecting.
export const ENV_FILE = "/etc/arker-compat.env";
// Files that may hold env values. Commands run as root and /root is private to
// it, which a file's own mode cannot do: sync does not set modes.
const PRIVATE_DIR = "/root/.arker-compat";
// One directory per started process or terminal, named by its Arker session:
// its pid, how it was started, and its stdin pipe.
const PROCESS_DIR = `${PRIVATE_DIR}/sessions`;
// Marks the Arker sessions that stand for a provider's named sessions.
const SESSION_NAME_ENV = "ARKER_COMPAT_SESSION";
const SESSION_SETUP = `[ -f ${ENV_FILE} ] && . ${ENV_FILE}; true`;
// Type, size, permission bits, owner, group, mtime, link target, path; NUL-ended
// since paths may hold tabs and newlines.
const FIND_FORMAT = "'%y\\t%s\\t%m\\t%u\\t%g\\t%T@\\t%l\\t%p\\0'";
const TIMEOUT_RULE = "compat-timeout";
const SUSPEND_RULE = "compat-suspend";
const EGRESS_RULE = "compat-egress";
const PORT_RULE = "compat-port";
// Inbound rules that let in requests carrying a token: signed file and port URLs.
const GRANT_RULE = "compat-grant";
// Above every port rule, so a request with a token is let in before a port rule refuses it.
const GRANT_PRIORITY = 200_000;
// Where the server behind signed file URLs listens in the VM.
const FILE_SERVER_PORT = 49983;
const FILE_SERVER_PATH = "/opt/arker-compat/file_server.py";
const MIB = 1024 * 1024;
const LIST_PAGE_SIZE = 1000;
// Rules of one type need distinct priorities; ports are at most 65535.
const PORT_RULE_PRIORITY = 100_000;
// The API's limit on a VM description, where sandbox metadata is kept.
const MAX_DESCRIPTION = 1024;
const POLL_INTERVAL_MS = 500;

const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const TIMED_OUT = /command timed out after \d+ms\n?$/;
const decoder = new TextDecoder();

type ErrorClass = new (message: string) => Error;

/** The provider's error classes, thrown by the core in its place. */
export interface Errors {
  provider: string;
  unsupported: ErrorClass;
  invalid: ErrorClass;
  notFound: ErrorClass;
  fileNotFound: ErrorClass;
  timeout: ErrorClass;
  failed: ErrorClass;
  auth?: ErrorClass;
  rateLimit?: ErrorClass;
}

export interface CommandResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  error?: string;
}

export interface Entry {
  name: string;
  path: string;
  isDir: boolean;
  size: number;
  mode: number;
  owner: string;
  group: string;
  /** Seconds since the epoch. */
  modified: number;
  symlinkTarget?: string;
}

export interface ProcessInfo {
  pid: number;
  sessionId: string;
  cmd: string;
  cwd?: string;
  envs: Record<string, string>;
  pty: boolean;
  name?: string;
  /** A terminal's size. */
  size?: { cols: number; rows: number };
}

/** A difference between two listings; see {@link CompatSandbox.changes}. */
export interface Change {
  path: string;
  kind: "create" | "write" | "chmod" | "remove";
}

/** `rwxr-xr-x` style. */
export function permissions(mode: number): string {
  const special = [
    [0o4000, 2, "s"],
    [0o2000, 5, "s"],
    [0o1000, 8, "t"],
  ] as const;
  const chars = [..."rwxrwxrwx"].map((char, index) => (mode & (1 << (8 - index)) ? char : "-"));
  for (const [bit, index, char] of special) {
    if (mode & bit) chars[index] = chars[index] === "x" ? char : char.toUpperCase();
  }
  return chars.join("");
}

function entryFrom(line: string): Entry {
  const [kind, size, mode, owner, group, modified, target, ...rest] = line.split("\t");
  const path = rest.join("\t");
  return {
    name: withoutTrailingSlashes(path).split("/").pop() || path,
    path,
    isDir: kind === "d",
    size: Number(size),
    mode: parseInt(mode!, 8),
    owner: owner!,
    group: group!,
    modified: Number(modified),
    ...(target ? { symlinkTarget: target } : {}),
  };
}

const processDir = (sessionId: string) => `${PROCESS_DIR}/${sessionId}`;

/** Shell lines making `directory/stdin` a pipe that stays open until {@link releaseStdin}. */
function openStdin(directory: string): string {
  return `rm -f ${directory}/stdin; mkfifo ${directory}/stdin && (sleep infinity > ${directory}/stdin & echo $! > ${directory}/holder)\n`;
}

function releaseStdin(directory: string): string {
  return `kill $(cat ${directory}/holder 2>/dev/null) 2>/dev/null; rm -f ${directory}/stdin`;
}

/** `cmd` run in the session's own shell with stdin from a pipe. */
function piped(directory: string, cmd: string): string {
  return (
    `mkdir -p ${directory}\n${openStdin(directory)}{\n${cmd}\n} < ${directory}/stdin; ` +
    `__arker_status=$?; ${releaseStdin(directory)}; (exit $__arker_status)`
  );
}

export type OutputCallback = (data: string) => void | Promise<void>;

export function arkerSource(config: CompatConfig = {}): string {
  return config.source || env("ARKER_SOURCE") || env("ARKER_SOURCE_VM") || DEFAULT_SOURCE;
}

export function shellQuote(value: string): string {
  if (value === "") return "''";
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

const DOCS = "https://arker.ai/docs";
const ISSUES = "https://github.com/ArkerHQ/arker-sdk/issues";

/** The error for anything a layer does not cover: what it was, what to do instead, and where to ask for it. */
export function unsupported(errors: Errors, feature: string, hint?: string): Error {
  const lines = [`${feature} is not supported by Arker's ${errors.provider} compatibility layer yet.`];
  if (hint) lines.push(`  Instead: ${hint}`);
  lines.push(
    `  Use Arker directly: the sandbox's \`.arker\` property is its Arker VM (${DOCS}/quickstart).`,
    `  What this layer covers: ${DOCS}/migration#migrate-from-${errors.provider.toLowerCase()}`,
    `  Need it? Open an issue: ${ISSUES}`,
  );
  return new errors.unsupported(lines.join("\n"));
}

/**
 * Outbound traffic, as sandbox providers describe it: an allow beats a deny,
 * and what neither names is allowed unless `denyAll`. `allowHosts` are exact
 * names, or `*.name` for one label under it.
 */
export interface Egress {
  allowIps?: string[];
  allowHosts?: string[];
  denyIps?: string[];
  denyAll?: boolean;
}

const IPV4_OCTET = "(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)";
const IPV4 = new RegExp(`^${IPV4_OCTET}(\\.${IPV4_OCTET}){3}$`);

/** An IPv4 or IPv6 address or CIDR block, as Python's `ipaddress.ip_network(entry, strict=False)` reads one. */
function isIpOrCidr(entry: string): boolean {
  const [address = "", prefix, ...rest] = entry.split("/");
  const bits = IPV4.test(address) ? 32 : address.includes(":") && isIpv6(address) ? 128 : 0;
  return bits > 0 && rest.length === 0 && (prefix === undefined || (/^\d+$/.test(prefix) && Number(prefix) <= bits));
}

function isIpv6(address: string): boolean {
  try {
    new URL(`http://[${address}]`);
    return true;
  } catch {
    return false;
  }
}

/** Split entries into `[ipsAndCidrs, hostnames]`. */
export function splitAddresses(entries: string[]): [string[], string[]] {
  return [entries.filter(isIpOrCidr), entries.filter((entry) => !isIpOrCidr(entry))];
}

/**
 * Arker outbound rules, highest priority first. Arker matches a host and every
 * name under it, so each exact host has a deny for `*.host` just above; deeper
 * names come first so that deny never hides another allow. `*.` covers one
 * label, so names two or more labels under an exact host stay allowed.
 */
export function egressRules(egress: Egress): Record<string, unknown>[] {
  const rule = (action: string, match?: Record<string, string[]>) => ({ type: "outbound", action, ...(match ? { match } : {}) });
  const hosts = egress.allowHosts ?? [];
  const wildcards = hosts.filter((host) => host.startsWith("*."));
  const exact = hosts.filter((host) => !host.startsWith("*.")).sort((a, b) => b.split(".").length - a.split(".").length);
  const rules = wildcards.length > 0 ? [rule("allow", { hosts: wildcards })] : [];
  for (const host of exact) rules.push(rule("deny", { hosts: [`*.${host}`] }), rule("allow", { hosts: [host] }));
  if (egress.allowIps?.length) rules.push(rule("allow", { ips: egress.allowIps }));
  if (egress.denyIps?.length) rules.push(rule("deny", { ips: egress.denyIps }));
  if (egress.denyAll) rules.push(rule("deny"));
  // An Arker allow rule denies what no rule matches; providers allow it.
  else if (rules.length > 0) rules.push(rule("allow"));
  return rules;
}

function egressPolicy(egress: Egress): Record<string, unknown> {
  const rules = egressRules(egress);
  return Object.fromEntries(rules.map((rule, index) => [`${EGRESS_RULE}-${index}`, { ...rule, priority: (rules.length - index) * 10 }]));
}

const warned = new Set<string>();

/**
 * Throw for options the layer cannot honour. `ignored` options only configure
 * the provider's own client; `inert` ones have no Arker meaning, and setting
 * one warns once.
 */
export function rejectOptions(
  errors: Errors,
  method: string,
  options: Record<string, unknown>,
  ignored: ReadonlySet<string> = new Set(),
  inert: ReadonlySet<string> = new Set(),
): void {
  const given = Object.entries(options)
    .filter(([name, value]) => value !== undefined && value !== null && !ignored.has(name))
    .map(([name]) => name)
    .sort();
  for (const name of given.filter((name) => inert.has(name))) {
    const feature = `${method}({ ${name} })`;
    if (warned.has(feature)) continue;
    warned.add(feature);
    console.warn(`${feature} has no effect on Arker and is ignored.`);
  }
  const rejected = given.filter((name) => !inert.has(name));
  if (rejected.length > 0) throw unsupported(errors, `${method}({ ${rejected.join(", ")} })`);
}

/** The original provider's package, or undefined when it is not installed. */
export async function importOptional<T>(name: string): Promise<T | undefined> {
  try {
    return (await import(/* @vite-ignore */ name)) as T;
  } catch (error) {
    const code = (error as { code?: string }).code;
    if (code === "ERR_MODULE_NOT_FOUND" || code === "MODULE_NOT_FOUND") return undefined;
    throw error;
  }
}

// Properties tools read to learn what an object is (promises, test matchers,
// serializers, inspectors); a missing one must read as undefined, not throw.
const PROBES = new Set(["then", "toJSON", "asymmetricMatch", "$$typeof", "nodeType", "constructor", "inspect"]);

/** Throw a clear error, rather than `undefined is not a function`, for what a layer does not cover. */
export function guardUnsupported<T extends object>(
  instance: T,
  errors: Errors,
  path: string,
  hints: Record<string, string> = {},
): T {
  return new Proxy(instance, {
    get(target, prop) {
      if (typeof prop === "symbol" || prop in target || PROBES.has(prop)) {
        const value = Reflect.get(target, prop, target);
        return typeof value === "function" ? value.bind(target) : value;
      }
      throw unsupported(errors, `${path}.${prop}`, hints[prop]);
    },
  });
}

/** A loop, not `/\/+$/`, which takes quadratic time on a long run of slashes. */
export function withoutTrailingSlashes(text: string): string {
  let end = text.length;
  while (end > 0 && text[end - 1] === "/") end--;
  return text.slice(0, end);
}

function env(key: string): string | undefined {
  const value = globalThis.process?.env?.[key]?.trim();
  return value ? value : undefined;
}

function scheduleRule(action: string, clock: { after_created: number } | { at: string }) {
  return { type: "schedule", ...clock, action };
}

function exportsFor(errors: Errors, envs: Record<string, string>): string {
  const invalid = Object.keys(envs).filter((name) => !ENV_NAME.test(name));
  if (invalid.length > 0) {
    throw new errors.invalid(`Invalid environment variable name(s): ${invalid.map((name) => `'${name}'`).join(", ")}`);
  }
  return Object.entries(envs)
    .map(([name, value]) => `export ${name}=${shellQuote(String(value))}\n`)
    .join("");
}

function portHostOf(hostname: string | null | undefined, port: number): string | undefined {
  if (!hostname) return undefined;
  const dot = hostname.indexOf(".");
  return `${hostname.slice(0, dot)}-${port}${hostname.slice(dot)}`;
}

/** The header and token every request to a sandbox's exposed ports must carry. */
export interface PortAuth {
  header: string;
  token: string;
}

export function newPortAuth(header: string): PortAuth {
  return { header, token: randomBytes(32).toString("base64url") };
}

type Rules = Record<string, { type?: string; priority?: number; expires_at?: string | null } | null>;

/** Whether the outbound rules a compatibility layer set on `vm` deny all traffic. */
export function blocksAllEgress(vm: VM): boolean {
  const rules = ((vm as VM & Vm).policies?.rules ?? {}) as Record<string, { action?: unknown; match?: Record<string, unknown> | null }>;
  const egress = Object.entries(rules).filter(([id]) => id.startsWith(`${EGRESS_RULE}-`)).map(([, rule]) => rule);
  if (egress.length !== 1 || egress[0]!.action !== "deny") return false;
  // A catch-all rule reads back with a match whose fields are all unset.
  return Object.values(egress[0]!.match ?? {}).every((value) => value === null || value === undefined);
}

function grantId(kind: string, port: number, token: string): string {
  return `${GRANT_RULE}-${kind}-${port}-${createHash("sha256").update(token).digest("hex").slice(0, 16)}`;
}

function killBound(timeout: number | undefined): number | undefined {
  return timeout ? Math.ceil(timeout) : undefined;
}

function isMissing(error: unknown, ...statuses: number[]): boolean {
  return error instanceof ArkerError && statuses.includes(error.status);
}

function descriptionJson(errors: Errors, described: Record<string, unknown>): string {
  const text = JSON.stringify(described);
  if (text.length > MAX_DESCRIPTION) throw new errors.invalid(`Sandbox metadata is too large: at most ${MAX_DESCRIPTION} characters as JSON.`);
  return text;
}

/** Parse the exports either SDK writes to {@link ENV_FILE}, as Python's `shlex.split` does. */
function parseExports(errors: Errors, text: string): Record<string, string> {
  const words = shellWords(text);
  if (!words) throw new errors.failed(`${ENV_FILE} is not a list of exports`);
  return Object.fromEntries(words.filter((word) => word !== "export").map((word) => /^([^=]*)=?(.*)$/s.exec(word)!.slice(1)));
}

/** The words of POSIX shell `text` (quotes and escapes, no expansions); undefined if a quote is not closed. */
function shellWords(text: string): string[] | undefined {
  const token = /'([^']*)'|"((?:[^"\\]|\\.)*)"|\\(.)|(\s+)|([^\s'"\\]+)/sy;
  const words: string[] = [];
  let word: string | undefined;
  while (token.lastIndex < text.length) {
    const match = token.exec(text);
    if (!match) return undefined;
    const [, single, double, escaped, space, bare] = match;
    if (space !== undefined) {
      if (word !== undefined) words.push(word);
      word = undefined;
    } else {
      word = (word ?? "") + (single ?? double?.replace(/\\([\\"$`\n])/g, "$1") ?? escaped ?? bare);
    }
  }
  if (word !== undefined) words.push(word);
  return words;
}

/** The VM a sandbox is forked from, as it is now; undefined when there is no such VM, which the fork itself then reports. */
async function sourceVm(errors: Errors, config: CompatConfig = {}, parent?: VM, source?: string): Promise<VM | undefined> {
  const { source: _source, name: _name, ...arkerOptions } = config;
  try {
    return parent ? await parent.refresh() : await new Arker(arkerOptions).getVm(source || arkerSource(config));
  } catch (error) {
    if (isMissing(error, 400, 404)) return undefined;
    throw providerError(errors, error);
  }
}

function layerRuleIds(vm: VM | undefined): string[] {
  const rules = (vm as (VM & Vm) | undefined)?.policies?.rules ?? {};
  const prefixes = [TIMEOUT_RULE, SUSPEND_RULE, `${EGRESS_RULE}-`, `${PORT_RULE}-`, `${GRANT_RULE}-`];
  return Object.keys(rules).filter((id) => prefixes.some((prefix) => id.startsWith(prefix)));
}

function description(vm: VM): Record<string, any> {
  try {
    const parsed = JSON.parse((vm as VM & Vm).description || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

/** An Arker API error the caller did not handle, as the provider's error. */
function providerError(errors: Errors, error: unknown): unknown {
  if (!(error instanceof ArkerError)) return error;
  const kind =
    (error.status === 401 || error.status === 403) && errors.auth
      ? errors.auth
      : error.status === 429 && errors.rateLimit
        ? errors.rateLimit
        : errors.failed;
  return Object.assign(new kind(error.message), { cause: error });
}

async function translated<T>(errors: Errors, work: () => Promise<T>): Promise<T> {
  try {
    return await work();
  } catch (error) {
    throw providerError(errors, error);
  }
}

/** Every item of a cursor-paginated listing. */
async function* paged<T>(fetch: (cursor: string | undefined) => Promise<[T[], string | null | undefined]>): AsyncGenerator<T> {
  let cursor: string | undefined;
  do {
    const [items, next] = await fetch(cursor);
    yield* items;
    cursor = next ?? undefined;
  } while (cursor);
}

async function emit(callback: OutputCallback | undefined, text: string): Promise<void> {
  if (callback && text) await callback(text);
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * A command started in a session of its own; see {@link CompatSandbox.start}.
 *
 * The session is closed when the command ends, and {@link Process.kill}
 * closes it early, which stops everything the command started.
 */
export class Process {
  #stdout: Uint8Array = new Uint8Array();
  #stderr: Uint8Array = new Uint8Array();
  #state = "running";
  #exitCode: number | null | undefined;
  #failReason: string | null | undefined;

  #pid: number | undefined;

  /** A process in a session it does not own (`ownsSession: false`) leaves the session alone. */
  constructor(
    private readonly sandbox: CompatSandbox,
    private readonly errors: Errors,
    readonly runId: string,
    readonly sessionId: string,
    private readonly timeout: number | undefined,
    private readonly options: { ownsSession?: boolean; stdin?: boolean } = {},
  ) {}

  /** The command's process id in the VM, once it has started. */
  async pid(): Promise<number> {
    while (this.#pid === undefined) {
      this.#pid = await this.sandbox.processPid(this.sessionId);
      if (this.#pid !== undefined) break;
      await this.#refresh();
      if (this.done) {
        // It may have recorded its pid and exited since the read above.
        this.#pid = await this.sandbox.processPid(this.sessionId);
        if (this.#pid !== undefined) break;
        this.result();
        throw new this.errors.failed("The command ended before it reported its process id.");
      }
      await sleep(POLL_INTERVAL_MS / 5);
    }
    return this.#pid;
  }

  async sendStdin(data: string | Uint8Array): Promise<void> {
    if (!(await this.#oncePiped(() => this.sandbox.sendStdin(this.sessionId, data)))) {
      throw new this.errors.invalid("The command has ended, or its stdin is closed.");
    }
  }

  /** Close stdin, so the command reads end of file; nothing to do once it has ended. */
  async closeStdin(): Promise<void> {
    await this.#oncePiped(() => this.sandbox.closeStdin(this.sessionId));
  }

  /** Do `action` once the command has set up its stdin pipe; false if it ended first. */
  async #oncePiped(action: () => Promise<boolean>): Promise<boolean> {
    if (!this.options.stdin) throw new this.errors.invalid("The command was started without stdin.");
    while (!(await action())) {
      await this.#refresh();
      if (this.done) return false;
      await sleep(POLL_INTERVAL_MS / 5);
    }
    return true;
  }

  get done(): boolean {
    return this.#state !== "pending" && this.#state !== "running";
  }

  /** The exit code once the command has ended. */
  get exitCode(): number | undefined {
    return this.done ? (this.#exitCode ?? undefined) : undefined;
  }

  /** Output fetched so far. */
  get stdout(): string {
    return decoder.decode(this.#stdout);
  }

  get stderr(): string {
    return decoder.decode(this.#stderr);
  }

  async #refresh(): Promise<void> {
    if (this.done) return;
    const run = await translated(this.errors, () => this.sandbox.vm.getRun(this.runId));
    this.#stdout = run.stdoutBytes;
    this.#stderr = run.stderrBytes;
    this.#state = run.state;
    this.#exitCode = run.exit_code;
    this.#failReason = run.fail_reason;
    if (this.done && this.options.ownsSession !== false) await this.#closeSession();
  }

  async #closeSession(): Promise<boolean> {
    return translated(this.errors, async () => {
      try {
        await this.sandbox.vm.deleteSession(this.sessionId);
      } catch (error) {
        if (isMissing(error, 404)) return false;
        throw error;
      }
      return true;
    });
  }

  /** The exit code, or null while the command runs. */
  async poll(): Promise<number | null> {
    await this.#refresh();
    return this.done ? this.result().exitCode : null;
  }

  /**
   * Yield `[stdout, stderr]` text as it arrives, from the start of the output,
   * until the command ends. Each call reads independently.
   */
  async *follow(): AsyncGenerator<[string, string]> {
    const decoders = [new TextDecoder(), new TextDecoder()];
    const offsets = [0, 0];
    for (;;) {
      await this.#refresh();
      const chunk = [this.#stdout, this.#stderr].map((data, index) => {
        const text = decoders[index]!.decode(data.subarray(offsets[index]), { stream: !this.done });
        offsets[index] = data.length;
        return text;
      }) as [string, string];
      if (chunk[0] || chunk[1]) yield chunk;
      if (this.done) return;
      await sleep(POLL_INTERVAL_MS);
    }
  }

  async wait(onStdout?: OutputCallback, onStderr?: OutputCallback): Promise<CommandResult> {
    for await (const [stdout, stderr] of this.follow()) {
      await emit(onStdout, stdout);
      await emit(onStderr, stderr);
    }
    return this.result();
  }

  result(): CommandResult {
    return this.sandbox.finish(this.stdout, this.stderr, this.#exitCode, this.#state, this.#failReason, this.timeout);
  }

  /** Stop the command and everything it started; false if it had already ended. */
  async kill(): Promise<boolean> {
    if (this.done) return false;
    const killed = await this.#closeSession();
    await this.#refresh();
    return killed;
  }
}

export interface CreateOptions {
  config?: CompatConfig;
  source?: string;
  image?: string;
  envs?: Record<string, string>;
  cpus?: number;
  memoryMiB?: number;
  diskMiB?: number;
  /** Seconds after creation when the VM is deleted, or suspended with `onTimeout: "suspend"`. */
  deleteAfter?: number;
  onTimeout?: "delete" | "suspend";
  egress?: Egress;
  metadata?: object;
  name?: string;
  /** An environment variable that commands see holding the sandbox id. */
  idEnv?: string;
  /** Fork this VM. */
  parent?: VM;
  /** Keep the exposed ports and outbound rules of the VM forked; see {@link CompatSandbox.create}. */
  keepNetwork?: boolean;
  /** Mark the new VM as a snapshot of this sandbox, listed apart from sandboxes. */
  snapshotOf?: string;
  /** Mount paths, and the ids of the volumes to mount there. */
  volumes?: Record<string, string>;
  /** Exposed ports let in only requests carrying this token; see {@link newPortAuth}. */
  portAuth?: PortAuth;
}

/** One Arker VM, driven the way sandbox providers expect. */
export class CompatSandbox {
  readonly id: string;
  readonly #onTimeout: "delete" | "suspend";
  /** The metadata as of loading the VM; {@link metadata} reads it fresh. */
  settings: Record<string, any>;
  readonly snapshotOf: string | undefined;
  /** The header and token every request to an exposed port must carry, if any. */
  readonly portAuth: PortAuth | undefined;

  constructor(
    readonly vm: VM,
    /** @internal */ readonly errors: Errors,
  ) {
    const described = description(vm);
    this.id = vm.id;
    this.#onTimeout = described.on_timeout === "suspend" ? "suspend" : "delete";
    this.settings = described.compat ?? {};
    this.snapshotOf = described.snapshot_of;
    this.portAuth = described.port_auth;
  }

  /**
   * Fork `parent`, else `image`, else `source`, else the configured default VM.
   * A sandbox forked from a VM keeps its sandbox envs. It takes none of the
   * rules this layer set on that VM, so it serves no port and limits no traffic
   * it did not ask for; with `keepNetwork` it keeps the VM's exposed ports and
   * outbound rules. `snapshotOf` marks the new VM as a snapshot of that sandbox.
   */
  static async create(errors: Errors, options: CreateOptions = {}): Promise<CompatSandbox> {
    const { config, source, image, envs, cpus, memoryMiB, diskMiB, deleteAfter, onTimeout = "delete", egress, metadata, idEnv, parent, snapshotOf, portAuth } =
      options;
    exportsFor(errors, envs ?? {});
    const forksVm = parent !== undefined || !image;
    const rules: Record<string, unknown> = {};
    if (forksVm) {
      // A fork's policies are merged into its source's, so the source's rules
      // apply to the fork unless replaced.
      for (const id of layerRuleIds(await sourceVm(errors, config, parent, source))) {
        const keep = id.startsWith(`${EGRESS_RULE}-`) ? options.keepNetwork && !egress : options.keepNetwork && id.startsWith(`${PORT_RULE}-`);
        if (!keep) rules[id] = null;
      }
    }
    if (deleteAfter) rules[TIMEOUT_RULE] = scheduleRule(onTimeout, { after_created: Math.ceil(deleteAfter) });
    if (egress) Object.assign(rules, egressPolicy(egress));
    const resources = {
      ...(cpus ? { vcpu: cpus } : {}),
      ...(memoryMiB ? { memory_mib: memoryMiB } : {}),
      ...(diskMiB ? { disk_mib: diskMiB } : {}),
    };
    const describedJson = descriptionJson(errors, {
      layer: errors.provider.toLowerCase(),
      ...(metadata ? { compat: metadata } : {}),
      ...(onTimeout !== "delete" ? { on_timeout: onTimeout } : {}),
      ...(snapshotOf ? { snapshot_of: snapshotOf } : {}),
      ...(portAuth ? { port_auth: portAuth } : {}),
    });
    const name = options.name ?? config?.name;
    const { source: _source, name: _name, ...arkerOptions } = config ?? {};
    const forkOptions = {
      ...(Object.keys(rules).length > 0 ? { policies: { rules } as never } : {}),
      ...(Object.keys(resources).length > 0 ? { resources } : {}),
      description: describedJson,
      ...(name ? { name } : {}),
    };
    let vm: VM;
    try {
      vm = parent
        ? await parent.fork(forkOptions)
        : await new Arker(arkerOptions).fork({ ...(image ? { image } : { source_vm_name: source || arkerSource(config) }), ...forkOptions });
    } catch (error) {
      if (source && isMissing(error, 404)) {
        throw new errors.notFound(
          `No Arker VM named '${source}' to start from. Name a VM you have prepared, ` +
            "or omit it to start from the default image.",
        );
      }
      throw providerError(errors, error);
    }
    const sandbox = new CompatSandbox(vm, errors);
    const newEnvs = { ...envs, ...(idEnv ? { [idEnv]: vm.id } : {}) };
    try {
      if (Object.keys(newEnvs).length > 0) {
        const inherited = forksVm ? await sandbox.env() : {};
        await sandbox.write(ENV_FILE, exportsFor(errors, { ...inherited, ...newEnvs }));
      }
      for (const [path, volumeId] of Object.entries(options.volumes ?? {})) await sandbox.mount(volumeId, path);
    } catch (error) {
      await sandbox.discard();
      throw error;
    }
    return sandbox;
  }

  /** The sandboxes (or, with `snapshots`, the snapshots) this provider's layer created. */
  static async *find(errors: Errors, config: CompatConfig = {}, options: { snapshots?: boolean } = {}): AsyncGenerator<CompatSandbox> {
    const { source: _source, name: _name, ...arkerOptions } = config;
    const client = new Arker(arkerOptions);
    const layer = errors.provider.toLowerCase();
    // VMs created while paging can shift a later page onto ones already seen.
    const seen = new Set<string>();
    const vms = paged(async (cursor) => {
      const page = await translated(errors, () => client.listVms({ cursor, limit: LIST_PAGE_SIZE }));
      return [page.vms, page.nextCursor];
    });
    for await (const vm of vms) {
      const described = description(vm);
      if (seen.has(vm.id) || described.layer !== layer || "snapshot_of" in described !== Boolean(options.snapshots)) continue;
      seen.add(vm.id);
      yield new CompatSandbox(vm, errors);
    }
  }

  /**
   * A new sandbox from this one's disk and memory, with its metadata, envs,
   * exposed ports and outbound rules, as a copy of the sandbox would have.
   */
  async fork(options: CreateOptions = {}): Promise<CompatSandbox> {
    return CompatSandbox.create(this.errors, {
      metadata: await this.metadata(),
      onTimeout: this.#onTimeout,
      portAuth: this.portAuth,
      ...options,
      parent: this.vm,
      keepNetwork: true,
    });
  }

  /** A copy of this sandbox that new sandboxes can start from by its name, deleted `deleteAfter` seconds from now when given. */
  async snapshot(name: string | undefined, deleteAfter?: number): Promise<CompatSandbox> {
    return this.fork({ name, snapshotOf: this.id, deleteAfter, onTimeout: "delete" });
  }

  /** The sandbox with id or name `vmId`, or null when Arker has no such VM. */
  static async connect(errors: Errors, vmId: string, config: CompatConfig = {}): Promise<CompatSandbox | null> {
    const { source: _source, name: _name, ...arkerOptions } = config;
    try {
      const vm = await new Arker(arkerOptions).getVm(vmId);
      if (!vm.vm_id || vm.vm_id === vm.id) return new CompatSandbox(vm, errors);
      const { id: _id, baseUrl, ...data } = vm;
      return new CompatSandbox(new VM(vm._client, vm.vm_id, baseUrl, data as unknown as Vm), errors);
    } catch (error) {
      if (isMissing(error, 400, 404)) return null;
      throw providerError(errors, error);
    }
  }

  async metadata(): Promise<Record<string, any>> {
    return description(await this.info()).compat ?? {};
  }

  async info(): Promise<VM & Vm> {
    return translated(this.errors, async () => (await this.vm.refresh()) as VM & Vm);
  }

  /** Delete the VM while another error is on its way out, without hiding that error. */
  async discard(): Promise<void> {
    await this.vm.delete().catch(() => undefined);
  }

  async isAlive(): Promise<boolean> {
    return translated(this.errors, async () => {
      try {
        await this.vm.refresh();
      } catch (error) {
        if (isMissing(error, 404)) return false;
        throw error;
      }
      return true;
    });
  }

  /** False when the VM was already gone. Deleting a missing VM succeeds on Arker, so check that it exists first. */
  async delete(): Promise<boolean> {
    if (!(await this.isAlive())) return false;
    return translated(this.errors, async () => {
      try {
        await this.vm.delete();
      } catch (error) {
        if (isMissing(error, 404)) return false;
        throw error;
      }
      return true;
    });
  }

  async setTimeout(seconds: number): Promise<void> {
    const at = new Date(Date.now() + seconds * 1_000).toISOString();
    await this.#rules({ [TIMEOUT_RULE]: scheduleRule(this.#onTimeout, { at }) });
  }

  async clearTimeout(): Promise<void> {
    await this.#rules({ [TIMEOUT_RULE]: null });
  }

  /** Mount a volume at `path` and wait until it serves files. The VM attaches a mount when it next runs, so the wait runs in the VM. */
  async mount(volumeId: string, path: string): Promise<void> {
    const mount = await translated(this.errors, () => this.vm.createMount({ filesystemId: volumeId, path }));
    const polls = Math.ceil((MOUNT_WAIT_SECONDS * 1_000) / POLL_INTERVAL_MS);
    const quoted = shellQuote(path);
    const wait = `for _ in $(seq ${polls}); do mountpoint -q ${quoted} && exit 0; sleep ${POLL_INTERVAL_MS / 1_000}; done; exit 1`;
    if ((await this.run(wait)).exitCode !== 0) {
      const { mounts } = await translated(this.errors, () => this.vm.listMounts({ filesystemId: volumeId }));
      const current = mounts.find((candidate) => candidate.mount_id === mount.mount_id) ?? mount;
      throw new this.errors.failed(`Volume ${volumeId} did not mount at ${path}: ${current.state_detail || current.state}`);
    }
  }

  async restrictEgress(egress: Egress): Promise<void> {
    await this.#rules(egressPolicy(egress));
  }

  /** Suspend the VM now if it is running. It resumes when next used. */
  async suspend(): Promise<void> {
    await this.#rules({ [SUSPEND_RULE]: scheduleRule("suspend", { at: new Date().toISOString() }) });
  }

  async #rulesNamed(prefix: string) {
    const rules = (await this.info()).policies?.rules ?? {};
    return Object.entries(rules).filter(([id]) => id.startsWith(prefix));
  }

  /** Replace the outbound rules this layer set; undefined allows all traffic. */
  async updateEgress(egress: Egress | undefined): Promise<void> {
    const stale = Object.fromEntries((await this.#rulesNamed(`${EGRESS_RULE}-`)).map(([id]) => [id, null]));
    const rules = { ...stale, ...(egress ? egressPolicy(egress) : {}) };
    if (Object.keys(rules).length > 0) await this.#rules(rules);
  }

  /** Change who may reach the ports already exposed, keeping the addresses each admits. */
  async setPortsPublic(isPublic: boolean): Promise<void> {
    for (const [id, rule] of await this.#rulesNamed(`${PORT_RULE}-`)) {
      const fromIps = "match" in rule ? rule.match?.ips : undefined;
      await this.expose(Number(id.split("-").pop()), { public: isPublic, fromIps });
    }
  }

  /** Merge `changes` into the sandbox metadata and return all of it. */
  async updateMetadata(changes: Record<string, unknown>): Promise<Record<string, any>> {
    const described = description(await this.info());
    described.compat = { ...described.compat, ...changes };
    const text = descriptionJson(this.errors, described);
    await translated(this.errors, () => this.vm.update({ description: text }));
    this.settings = described.compat;
    return this.settings;
  }

  async resize(resources: { cpus?: number; memoryMiB?: number; diskMiB?: number }): Promise<void> {
    await translated(this.errors, () => this.vm.update({ vcpu: resources.cpus, memory_mib: resources.memoryMiB, disk_mib: resources.diskMiB }));
  }

  /** The environment variables every command in the sandbox sees. */
  async env(): Promise<Record<string, string>> {
    try {
      return parseExports(this.errors, decoder.decode(await this.read(ENV_FILE)));
    } catch (error) {
      if (error instanceof this.errors.fileNotFound) return {};
      throw error;
    }
  }

  async updateEnv(envs: Record<string, string>, unset: string[] = []): Promise<void> {
    const current = Object.fromEntries(Object.entries(await this.env()).filter(([name]) => !unset.includes(name)));
    await this.write(ENV_FILE, exportsFor(this.errors, { ...current, ...envs }));
  }

  async homeDir(): Promise<string> {
    return this.#shell('printf %s "$HOME"');
  }

  /** The directory commands start in when given none. */
  async workDir(): Promise<string> {
    return (await this.#shell("pwd")).trim();
  }

  async #rules(rules: Record<string, unknown>): Promise<void> {
    await translated(this.errors, () => this.vm.update({ policies: { rules } as never }));
  }

  /**
   * Open `port` (to callers in `fromIps` when given) and return its host name. A non-public port needs
   * `Authorization: Bearer $ARKER_API_KEY` from the VM owner's organization.
   */
  async expose(port: number, options: { public?: boolean; fromIps?: string[] } = {}): Promise<string> {
    const auth = options.public === false ? "arker" : "open";
    const match = {
      ports: [port],
      ...(options.fromIps?.length ? { ips: options.fromIps } : {}),
      ...(this.portAuth ? { headers: { [this.portAuth.header]: [this.portAuth.token] } } : {}),
    };
    const rule = { type: "inbound", priority: PORT_RULE_PRIORITY + port, match, action: "allow", auth };
    await this.#rules({ [`${PORT_RULE}-${port}`]: rule });
    return this.#knownPortHost(port);
  }

  async #knownPortHost(port: number): Promise<string> {
    const host = this.portHost(port) ?? portHostOf((await this.info()).hostname, port);
    if (!host) throw new this.errors.failed("This Arker region does not expose guest ports.");
    return host;
  }

  /**
   * Let requests to `port` that meet `match` in, for `expiresIn` seconds when given.
   * Grants that have expired are removed as this one is added.
   */
  async #grant(kind: string, port: number, token: string, match: Record<string, unknown>, expiresIn: number | undefined): Promise<void> {
    const rules = ((await this.info()).policies?.rules ?? {}) as Rules;
    const now = Date.now();
    const changes: Record<string, unknown> = {};
    for (const [id, rule] of Object.entries(rules)) {
      if (id.startsWith(GRANT_RULE) && rule?.expires_at && Date.parse(rule.expires_at) <= now) changes[id] = null;
    }
    const used = new Set(Object.values(rules).filter((rule) => rule?.type === "inbound").map((rule) => rule!.priority));
    let priority = GRANT_PRIORITY;
    while (used.has(priority)) priority++;
    changes[grantId(kind, port, token)] = {
      type: "inbound",
      priority,
      match: { ports: [port], ...match },
      action: "allow",
      ...(expiresIn ? { expires_at: new Date(now + expiresIn * 1_000).toISOString() } : {}),
    };
    await this.#rules(changes);
  }

  async #revoke(prefix: string): Promise<void> {
    const stale = Object.fromEntries((await this.#rulesNamed(prefix)).map(([id]) => [id, null]));
    if (Object.keys(stale).length > 0) await this.#rules(stale);
  }

  /**
   * A URL that needs no credentials: a GET of it downloads `path`, or with `write` a POST of it
   * (the file, or a multipart/form-data body holding it) uploads to `path`. It works for
   * `expiresIn` seconds when given, else until {@link revokeFileUrls}; forks and snapshots do
   * not inherit it.
   */
  async fileUrl(path: string, options: { write: boolean; expiresIn?: number }): Promise<string> {
    if (!path.startsWith("/")) throw new this.errors.invalid(`A file URL needs an absolute path, not '${path}'.`);
    await this.#serveFiles();
    const token = randomBytes(32).toString("base64url");
    const match = {
      methods: [options.write ? "POST" : "GET"],
      paths: ["/files"],
      query_params: { path: [path], signature: [token] },
    };
    await this.#grant("file", FILE_SERVER_PORT, token, match, options.expiresIn);
    const query = `path=${encodeURIComponent(path)}&signature=${token}`;
    return `https://${await this.#knownPortHost(FILE_SERVER_PORT)}/files?${query}`;
  }

  /** Make every URL from {@link fileUrl} stop working. */
  async revokeFileUrls(): Promise<void> {
    await this.#revoke(`${GRANT_RULE}-file-`);
  }

  /** Start the server behind file URLs, unless it is running. */
  async #serveFiles(): Promise<void> {
    await this.write(FILE_SERVER_PATH, FILE_SERVER);
    const listening = `(exec 3<>/dev/tcp/127.0.0.1/${FILE_SERVER_PORT}) 2>/dev/null`;
    await this.#shell(
      `${listening} && exit 0\n` +
        `setsid nohup python3 ${FILE_SERVER_PATH} ${FILE_SERVER_PORT} > /dev/null 2>&1 < /dev/null &\n` +
        `for _ in $(seq 100); do ${listening} && exit 0; sleep 0.1; done\n` +
        "echo 'The file server did not start.' >&2; exit 1",
    );
  }

  /** A URL to `port` that needs no other credential, and its token; see {@link revokePortUrl}. */
  async signedPortUrl(port: number, expiresIn: number | undefined): Promise<{ url: string; token: string }> {
    const token = randomBytes(32).toString("base64url");
    await this.#grant("port", port, token, { query_params: { arker_token: [token] } }, expiresIn);
    return { url: `https://${await this.#knownPortHost(port)}/?arker_token=${token}`, token };
  }

  async revokePortUrl(port: number, token: string): Promise<void> {
    await this.#revoke(grantId("port", port, token));
  }

  /** The host name that serves `port`, when this handle knows the VM's hostname. */
  portHost(port: number): string | undefined {
    return portHostOf((this.vm as VM & Vm).hostname, port);
  }

  /**
   * A new file only root can read, for values kept out of a command's text,
   * which the runs API returns to anyone in the organization who can list runs.
   * @internal
   */
  async privateFile(data: string): Promise<string> {
    const path = `${PRIVATE_DIR}/envs/${crypto.randomUUID().replaceAll("-", "")}`;
    await this.write(path, data);
    return path;
  }

  /** Shell lines exporting `envs` from a private file, removed once read. */
  async #loadEnvs(envs: Record<string, string> | undefined): Promise<string> {
    if (!envs || Object.keys(envs).length === 0) return "";
    const path = await this.privateFile(exportsFor(this.errors, envs));
    return `. ${path}; rm -f ${path}\n`;
  }

  async #script(cmd: string, cwd: string | undefined, envs: Record<string, string> | undefined, prelude = ""): Promise<string> {
    // A subshell, so cd and exports never leak into the next command.
    const cd = cwd ? `cd ${shellQuote(cwd)} || exit 1\n` : "";
    return `(\n[ -f ${ENV_FILE} ] && . ${ENV_FILE}\n${await this.#loadEnvs(envs)}${prelude}${cd}${cmd}\n)`;
  }

  /** Run `cmd` in bash and wait for it. A non-zero exit is a result, not an error. Timeouts are in seconds. */
  async run(cmd: string, options: { cwd?: string; envs?: Record<string, string>; timeout?: number } = {}): Promise<CommandResult> {
    const script = await this.#script(cmd, options.cwd, options.envs);
    const done = await translated(this.errors, () => this.vm.run(script, { timeout: killBound(options.timeout) }));
    return this.finish(done.stdout, done.stderr, done.exitCode, done.state, done.failReason, options.timeout);
  }

  /**
   * Start `cmd` in a session of its own, so other commands do not wait behind it.
   * With `stdin`, its standard input is a pipe fed by {@link CompatSandbox.sendStdin}.
   */
  async start(
    cmd: string,
    options: { cwd?: string; envs?: Record<string, string>; timeout?: number; stdin?: boolean } = {},
  ): Promise<Process> {
    return translated(this.errors, async () => {
      const session = await this.vm.createSession();
      const directory = processDir(session.session_id);
      await this.write(`${directory}/info`, JSON.stringify({ cmd, cwd: options.cwd ?? null, envs: options.envs ?? {} }));
      let prelude = `echo $BASHPID > ${directory}/pid || exit 1\n`;
      // The info holds the envs' values, so it goes when the command ends.
      let cleanup = `rm -f ${directory}/info; touch ${directory}/done`;
      let command = cmd;
      if (options.stdin) {
        cleanup = `${releaseStdin(directory)}; ${cleanup}`;
        command = `{\n${cmd}\n} < ${directory}/stdin`;
      }
      prelude += `trap ${shellQuote(cleanup)} EXIT\n` + (options.stdin ? openStdin(directory) : "");
      const started = await this.vm.run(await this.#script(command, options.cwd, options.envs, prelude), {
        session_id: session.session_id,
        timeout: killBound(options.timeout),
        time_to_background: 0,
      });
      return new Process(this, this.errors, started.runId, session.session_id, options.timeout, { stdin: options.stdin });
    });
  }

  /** @internal */
  async processPid(sessionId: string): Promise<number | undefined> {
    const pid = (await this.run(`cat ${processDir(sessionId)}/pid 2>/dev/null`)).stdout.trim();
    return pid ? Number(pid) : undefined;
  }

  /** Commands and terminals this layer started that are still running. */
  async processes(): Promise<ProcessInfo[]> {
    const listing = await this.#shell(
      `cd ${PROCESS_DIR} 2>/dev/null || exit 0\n` +
        "for s in *; do [ -e $s/done ] && continue; p=$(cat $s/pid 2>/dev/null) && kill -0 $p 2>/dev/null" +
        " || continue; printf '%s\\t%s\\n' $p $s; done",
    );
    const found: ProcessInfo[] = [];
    for (const line of listing.split("\n").filter(Boolean)) {
      const [pid, sessionId] = line.split("\t") as [string, string];
      let details;
      try {
        // Read apart from the listing, whose output the runs API keeps.
        details = JSON.parse(decoder.decode(await this.read(`${processDir(sessionId)}/info`)));
      } catch (error) {
        if (error instanceof this.errors.fileNotFound) continue;
        throw error;
      }
      found.push({
        pid: Number(pid),
        sessionId,
        cmd: details.cmd,
        ...(details.cwd ? { cwd: details.cwd } : {}),
        envs: details.envs ?? {},
        pty: Boolean(details.pty),
        ...(details.name ? { name: details.name } : {}),
        ...(details.size ? { size: { cols: details.size[0], rows: details.size[1] } } : {}),
      });
    }
    return found;
  }

  async findProcess(pid: number): Promise<ProcessInfo | undefined> {
    return (await this.processes()).find((info) => info.pid === pid);
  }

  /** The running command `pid`, or undefined if no such command runs. */
  async attach(pid: number): Promise<Process | undefined> {
    const info = await this.findProcess(pid);
    if (!info || info.pty) return undefined;
    const run = (await this.#runs("running")).find((run) => run.session_id === info.sessionId);
    return run ? new Process(this, this.errors, run.run_id, info.sessionId, undefined) : undefined;
  }

  /** End the session and everything running in it; false if it was gone. */
  async killSession(sessionId: string): Promise<boolean> {
    return translated(this.errors, async () => {
      try {
        await this.vm.deleteSession(sessionId);
      } catch (error) {
        if (isMissing(error, 404)) return false;
        throw error;
      }
      return true;
    });
  }

  async killProcess(pid: number): Promise<boolean> {
    const info = await this.findProcess(pid);
    return info !== undefined && this.killSession(info.sessionId);
  }

  /** Write to the stdin pipe of the command in the session; false if it has none open. */
  async sendStdin(sessionId: string, data: string | Uint8Array): Promise<boolean> {
    const encoded = Buffer.from(typeof data === "string" ? new TextEncoder().encode(data) : data).toString("base64");
    return this.#piped(sessionId, `echo ${encoded} | base64 -d > ${processDir(sessionId)}/stdin`);
  }

  async closeStdin(sessionId: string): Promise<boolean> {
    return this.#piped(sessionId, releaseStdin(processDir(sessionId)));
  }

  async #piped(sessionId: string, script: string): Promise<boolean> {
    const result = await this.run(`[ -p ${processDir(sessionId)}/stdin ] || exit 44; ${script}`);
    if (result.exitCode === 44) return false;
    if (result.exitCode !== 0) throw new this.errors.failed(result.stderr.trim() || `stdin failed with exit ${result.exitCode}`);
    return true;
  }

  async #runs(state?: string): Promise<{ run_id: string; session_id?: string | null; command?: string | null; exit_code: number | null; started_at: string }[]> {
    return translated(this.errors, async () => {
      const runs = [];
      for await (const run of paged(async (cursor) => {
        const page = await this.vm.listRuns({ cursor, state: state as never });
        return [page.runs, page.next_cursor];
      })) {
        runs.push(run);
      }
      return runs;
    });
  }

  /** Start an interactive shell in a session of its own and attach to it. */
  async openTerminal(options: {
    cols: number;
    rows: number;
    cwd?: string;
    envs?: Record<string, string>;
    onData?: (data: Uint8Array) => void | Promise<void>;
    name?: string;
  }): Promise<Terminal> {
    const { cols, rows, cwd, envs = {}, onData, name } = options;
    const session = await translated(this.errors, () => this.vm.createSession(cwd ? { cwd } : {}));
    const directory = processDir(session.session_id);
    const info = { cmd: "/bin/bash", cwd: cwd ?? null, envs, pty: true, name: name ?? null, size: [cols, rows] };
    await this.write(`${directory}/info`, JSON.stringify(info));
    // The session's shell is the terminal's: what this run sets up, the terminal keeps.
    const setup =
      `echo $$ > ${directory}/pid\n` +
      `trap ${shellQuote(`echo $? > ${directory}/exit; rm -f ${directory}/info`)} EXIT\n` +
      `[ -f ${ENV_FILE} ] && . ${ENV_FILE}\n${await this.#loadEnvs(envs)}echo $$`;
    const done = await translated(this.errors, () => this.vm.run(setup, { session_id: session.session_id }));
    return Terminal.attach(this, session.session_id, Number(done.stdout.trim()), { cols, rows, onData });
  }

  async attachTerminal(info: ProcessInfo, onData?: (data: Uint8Array) => void | Promise<void>): Promise<Terminal> {
    return Terminal.attach(this, info.sessionId, info.pid, { onData });
  }

  async resizeTerminal(sessionId: string, cols: number, rows: number): Promise<void> {
    await translated(this.errors, () => this.vm.updateSession(sessionId, { cols, rows }));
    const infoPath = `${processDir(sessionId)}/info`;
    const info = JSON.parse(decoder.decode(await this.read(infoPath)));
    await this.write(infoPath, JSON.stringify({ ...info, size: [cols, rows] }));
  }

  /** @internal */
  async terminalExitCode(sessionId: string): Promise<number | undefined> {
    const code = (await this.run(`cat ${processDir(sessionId)}/exit 2>/dev/null`)).stdout.trim();
    return code ? Number(code) : undefined;
  }

  /** Sessions made by {@link CompatSandbox.createNamedSession}, by name, to their Arker session id. */
  async namedSessions(): Promise<Map<string, string>> {
    return translated(this.errors, async () => {
      const named = new Map<string, string>();
      for await (const session of paged(async (cursor) => {
        const page = await this.vm.listSessions({ cursor });
        return [page.sessions, page.next_cursor];
      })) {
        const name = session.env?.[SESSION_NAME_ENV];
        if (name) named.set(name, session.session_id);
      }
      return named;
    });
  }

  /** A shell that keeps its directory and variables from one command to the next. */
  async createNamedSession(name: string): Promise<string> {
    if ((await this.namedSessions()).has(name)) throw new this.errors.invalid(`Session '${name}' already exists.`);
    return translated(this.errors, async () => {
      const session = await this.vm.createSession({ env: { [SESSION_NAME_ENV]: name } });
      await this.vm.run(SESSION_SETUP, { session_id: session.session_id });
      return session.session_id;
    });
  }

  /** Run `cmd` in the session's shell and wait; returns the run id and result. Timeouts are in seconds. */
  async runInSession(sessionId: string, cmd: string, timeout?: number): Promise<[string, CommandResult]> {
    const done = await translated(this.errors, () => this.vm.run(cmd, { session_id: sessionId, timeout: killBound(timeout) }));
    return [done.runId ?? "", this.finish(done.stdout, done.stderr, done.exitCode, done.state, done.failReason, timeout)];
  }

  /** Start `cmd` in the session's shell, its stdin a pipe fed by {@link CompatSandbox.sendStdin}. */
  async startInSession(sessionId: string, cmd: string): Promise<Process> {
    const started = await translated(this.errors, () =>
      this.vm.run(piped(processDir(sessionId), cmd), { session_id: sessionId, time_to_background: 0 }),
    );
    return this.sessionProcess(sessionId, started.runId);
  }

  sessionProcess(sessionId: string, runId: string): Process {
    return new Process(this, this.errors, runId, sessionId, undefined, { ownsSession: false, stdin: true });
  }

  /** `[run id, command, exit code]` of each command run in the session, oldest first. */
  async sessionCommands(sessionId: string): Promise<[string, string, number | null][]> {
    const [before, after] = piped(processDir(sessionId), "\0").split("\0") as [string, string];
    return (await this.#runs())
      .filter((run) => run.session_id === sessionId && run.command !== SESSION_SETUP)
      .sort((a, b) => a.started_at.localeCompare(b.started_at))
      .map((run) => {
        let command = run.command ?? "";
        if (command.startsWith(before) && command.endsWith(after)) command = command.slice(before.length, command.length - after.length);
        return [run.run_id, command, run.exit_code];
      });
  }

  /** @internal */
  finish(
    stdout: string,
    stderr: string,
    exitCode: number | null | undefined,
    state: string,
    failReason: string | null | undefined,
    timeout: number | undefined,
  ): CommandResult {
    if (state === "failed") throw new this.errors.failed(failReason || "The command could not be run.");
    if (state === "cancelled" || exitCode == null) throw new this.errors.failed("The command was killed.");
    if (timeout && exitCode === 124 && TIMED_OUT.test(stderr)) {
      throw new this.errors.timeout(`Command timed out after ${timeout}s.`);
    }
    return { stdout, stderr, exitCode };
  }

  /** Run a helper script this layer wrote; any failure is the layer's error. */
  async #shell(script: string): Promise<string> {
    const result = await this.run(script);
    if (result.exitCode !== 0) throw new this.errors.failed(result.stderr.trim() || `exit ${result.exitCode}`);
    return result.stdout;
  }

  async read(path: string): Promise<Uint8Array> {
    return translated(this.errors, async () => {
      try {
        return await this.vm.sync(path);
      } catch (error) {
        if (isMissing(error, 404)) throw new this.errors.fileNotFound(`File not found: ${path}`);
        throw error;
      }
    });
  }

  /** Write `data` to `path`, creating parent directories. */
  async write(path: string, data: Uint8Array | string): Promise<void> {
    await translated(this.errors, () => this.vm.sync(path, data));
  }

  /** Entries under directory `path`, `depth` levels deep (every level when null). */
  async list(path: string, depth: number | null = 1): Promise<Entry[]> {
    const quoted = shellQuote(path);
    const maxDepth = depth ? `-maxdepth ${Math.trunc(depth)} ` : "";
    const result = await this.run(`[ -d ${quoted} ] || exit 44; find ${quoted} -mindepth 1 ${maxDepth}-printf ${FIND_FORMAT}`);
    if (result.exitCode === 44) throw new this.errors.fileNotFound(`Directory not found: ${path}`);
    if (result.exitCode !== 0) throw new this.errors.failed(result.stderr.trim() || `list failed with exit ${result.exitCode}`);
    return result.stdout.split("\0").filter(Boolean).map(entryFrom);
  }

  async stat(path: string): Promise<Entry> {
    const quoted = shellQuote(path);
    const result = await this.run(`[ -e ${quoted} ] || [ -L ${quoted} ] || exit 44; find ${quoted} -maxdepth 0 -printf ${FIND_FORMAT}`);
    if (result.exitCode === 44) throw new this.errors.fileNotFound(`No such file or directory: ${path}`);
    if (result.exitCode !== 0) throw new this.errors.failed(result.stderr.trim() || `stat failed with exit ${result.exitCode}`);
    return entryFrom(result.stdout.replace(/\0$/, ""));
  }

  /**
   * What changed under `path` since `before` (an earlier result's entries, or
   * an empty map to start), and the entries now there.
   */
  async changes(path: string, before: Map<string, Entry>, recursive: boolean): Promise<[Change[], Map<string, Entry>]> {
    const after = new Map((await this.list(path, recursive ? null : 1)).map((entry) => [entry.path, entry]));
    const found: Change[] = [...before.keys()].filter((entryPath) => !after.has(entryPath)).map((entryPath) => ({ path: entryPath, kind: "remove" }));
    for (const [entryPath, entry] of after) {
      const old = before.get(entryPath);
      if (!old) found.push({ path: entryPath, kind: "create" });
      else if (old.size !== entry.size || old.modified !== entry.modified) found.push({ path: entryPath, kind: "write" });
      else if (old.mode !== entry.mode) found.push({ path: entryPath, kind: "chmod" });
    }
    return [found, after];
  }

  async pathExists(path: string): Promise<boolean> {
    return (await this.run(`test -e ${shellQuote(path)}`)).exitCode === 0;
  }

  async isDir(path: string): Promise<boolean> {
    return (await this.run(`test -d ${shellQuote(path)}`)).exitCode === 0;
  }

  /** Create `path` (and, with `parents`, its parents); false if it already existed. */
  async makeDir(path: string, options: { parents?: boolean } = {}): Promise<boolean> {
    const quoted = shellQuote(path);
    const mkdir = options.parents === false ? "mkdir" : "mkdir -p";
    return (await this.#shell(`[ -d ${quoted} ] && echo existed || ${mkdir} ${quoted}`)).trim() !== "existed";
  }

  /** Remove `path`; without `recursive`, a non-empty directory is an error. */
  async remove(path: string, options: { recursive?: boolean } = {}): Promise<void> {
    const quoted = shellQuote(path);
    if (options.recursive !== false) {
      await this.#shell(`rm -rf ${quoted}`);
      return;
    }
    const result = await this.run(`[ -e ${quoted} ] || exit 44; if [ -d ${quoted} ]; then rmdir ${quoted}; else rm ${quoted}; fi`);
    if (result.exitCode === 44) throw new this.errors.fileNotFound(`No such file or directory: ${path}`);
    if (result.exitCode !== 0) throw new this.errors.failed(result.stderr.trim() || `remove failed with exit ${result.exitCode}`);
  }

  async rename(oldPath: string, newPath: string): Promise<void> {
    await this.#shell(`mv ${shellQuote(oldPath)} ${shellQuote(newPath)}`);
  }
}

// Git: every provider's git module, on the git CLI in the VM.

// Never wait for a password prompt no one can answer.
const GIT = "GIT_TERMINAL_PROMPT=0 git";
// Answers git's credential request from the environment, so a password is
// never written to disk or into a remote URL.
const CREDENTIAL_HELPER = '!f() { echo "username=$ARKER_GIT_USERNAME"; echo "password=$ARKER_GIT_PASSWORD"; }; f';

export interface GitFileChange {
  name: string;
  indexStatus: string;
  worktreeStatus: string;
  renamedFrom?: string;
}

export interface GitState {
  currentBranch?: string;
  upstream?: string;
  ahead: number;
  behind: number;
  detached: boolean;
  files: GitFileChange[];
}

/** Parse `git status --porcelain=1 --branch -z`: NUL-separated, paths unquoted, and a rename's source as the entry after it. */
function parseGitStatus(output: string): GitState {
  const entries = output.split("\0");
  const state: GitState = { ahead: 0, behind: 0, detached: false, files: [] };
  if (entries[0]?.startsWith("## ")) {
    const header = entries.shift()!.slice(3);
    const bracket = header.indexOf(" [");
    const head = (bracket === -1 ? header : header.slice(0, bracket)).replace(/^(No commits yet on |Initial commit on )/, "");
    if (head.startsWith("HEAD")) state.detached = true;
    else {
      const [branch, upstream] = head.split("...");
      state.currentBranch = branch;
      if (upstream) state.upstream = upstream;
    }
    for (const part of bracket === -1 ? [] : header.slice(bracket + 2, -1).split(", ")) {
      const [word, count] = part.split(" ");
      if (word === "ahead") state.ahead = Number(count);
      if (word === "behind") state.behind = Number(count);
    }
  }
  for (let index = 0; index < entries.length; index++) {
    const entry = entries[index]!;
    if (!entry) continue;
    const renamed = /[RC]/.test(entry.slice(0, 2));
    state.files.push({
      name: entry.slice(3),
      indexStatus: entry[0]!,
      worktreeStatus: entry[1]!,
      ...(renamed ? { renamedFrom: entries[++index]! } : {}),
    });
  }
  return state;
}

export interface GitOptions {
  envs?: Record<string, string>;
  cwd?: string;
  timeout?: number;
}

interface GitRunOptions extends GitOptions {
  username?: string;
  password?: string;
  check?: boolean;
}

/** Git operations in a sandbox. A failed git command throws `fail(result)`, or the provider's error naming git's own message. */
export class CompatGit {
  readonly #fail: (result: CommandResult) => Error;
  readonly #errors: Errors;

  constructor(
    private readonly sandbox: CompatSandbox,
    private readonly cwd?: string,
    fail?: (result: CommandResult) => Error,
  ) {
    this.#errors = sandbox.errors;
    this.#fail = fail ?? ((result) => new sandbox.errors.failed(result.stderr.trim() || result.stdout.trim()));
  }

  /** `git [-C path] args`; with a username or password, git asks for them only through the environment. */
  async run(args: string[], path?: string, options: GitRunOptions = {}): Promise<CommandResult> {
    const { username, password, check = true, envs, cwd, timeout } = options;
    const gitOptions = path ? ["-C", path] : [];
    const env: Record<string, string> = { ...envs };
    if (username !== undefined || password !== undefined) {
      gitOptions.push("-c", "credential.helper=", "-c", `credential.helper=${CREDENTIAL_HELPER}`);
      Object.assign(env, { ARKER_GIT_USERNAME: username ?? "", ARKER_GIT_PASSWORD: password ?? "" });
    }
    const command = `${GIT} ${[...gitOptions, ...args].map(shellQuote).join(" ")}`;
    const result = await this.sandbox.run(command, { cwd: cwd ?? this.cwd, envs: env, timeout });
    if (check && result.exitCode !== 0) throw this.#fail(result);
    return result;
  }

  async clone(
    url: string,
    path: string | undefined,
    options: GitRunOptions & { branch?: string; depth?: number; commit?: string; insecureSkipTls?: boolean } = {},
  ): Promise<CommandResult> {
    const { branch, depth, commit, insecureSkipTls, ...rest } = options;
    const args = [...(insecureSkipTls ? ["-c", "http.sslVerify=false"] : []), "clone"];
    if (branch) args.push("--branch", this.#ref(branch));
    if (depth) args.push("--depth", String(depth));
    args.push("--", url, ...(path ? [path] : []));
    const result = await this.run(args, undefined, rest);
    if (commit) {
      const target = path ?? withoutTrailingSlashes(url).split("/").pop()!.replace(/\.git$/, "");
      await this.run(["checkout", this.#ref(commit)], target, withoutCredentials(rest));
    }
    return result;
  }

  /** `name`, refused if git would read it as an option. */
  #ref(name: string): string {
    if (name.startsWith("-")) throw new this.#errors.invalid(`Invalid git branch or ref '${name}': it starts with '-'.`);
    return name;
  }

  async init(path: string, options: GitOptions & { bare?: boolean; initialBranch?: string } = {}): Promise<CommandResult> {
    const { bare, initialBranch, ...rest } = options;
    return this.run(["init", ...(bare ? ["--bare"] : []), ...(initialBranch ? ["--initial-branch", initialBranch] : []), path], undefined, rest);
  }

  async status(path: string, options: GitOptions = {}): Promise<GitState> {
    return parseGitStatus((await this.run(["status", "--porcelain=1", "--branch", "-z"], path, options)).stdout);
  }

  /** Local branch names, and the checked-out one. */
  async branches(path: string, options: GitOptions = {}): Promise<{ branches: string[]; current?: string }> {
    const output = (await this.run(["branch", "--format=%(HEAD) %(refname:short)"], path, options)).stdout;
    const lines = output.split("\n").filter((line) => line.trim());
    const current = lines.find((line) => line.startsWith("* "))?.slice(2);
    return { branches: lines.map((line) => line.slice(2)), ...(current ? { current } : {}) };
  }

  async createBranch(path: string, name: string, options: GitOptions = {}): Promise<CommandResult> {
    return this.run(["checkout", "-b", this.#ref(name)], path, options);
  }

  async checkoutBranch(path: string, name: string, options: GitOptions = {}): Promise<CommandResult> {
    return this.run(["checkout", this.#ref(name)], path, options);
  }

  async deleteBranch(path: string, name: string, options: GitOptions & { force?: boolean } = {}): Promise<CommandResult> {
    const { force, ...rest } = options;
    return this.run(["branch", force ? "-D" : "-d", this.#ref(name)], path, rest);
  }

  async add(path: string, files?: string[], options: GitOptions = {}): Promise<CommandResult> {
    return this.run(files?.length ? ["add", "--", ...files] : ["add", "--all"], path, options);
  }

  async commit(
    path: string,
    message: string,
    options: GitOptions & { authorName?: string; authorEmail?: string; allowEmpty?: boolean } = {},
  ): Promise<CommandResult> {
    const { authorName, authorEmail, allowEmpty, ...rest } = options;
    const identity = [...(authorName ? ["-c", `user.name=${authorName}`] : []), ...(authorEmail ? ["-c", `user.email=${authorEmail}`] : [])];
    return this.run([...identity, "commit", "-m", message, ...(allowEmpty ? ["--allow-empty"] : [])], path, rest);
  }

  async reset(path: string, options: GitOptions & { mode?: string; target?: string; paths?: string[] } = {}): Promise<CommandResult> {
    const { mode, target, paths, ...rest } = options;
    const args = ["reset", ...(mode ? [`--${mode}`] : []), ...(target ? [this.#ref(target)] : []), ...(paths?.length ? ["--", ...paths] : [])];
    return this.run(args, path, rest);
  }

  async restore(
    path: string,
    paths: string[],
    options: GitOptions & { staged?: boolean; worktree?: boolean; source?: string } = {},
  ): Promise<CommandResult> {
    const { staged, worktree, source, ...rest } = options;
    const args = ["restore", ...(staged ? ["--staged"] : []), ...(worktree ? ["--worktree"] : []), ...(source ? [`--source=${source}`] : [])];
    return this.run([...args, "--", ...paths], path, rest);
  }

  async push(path: string, options: GitRunOptions & { remote?: string; branch?: string; setUpstream?: boolean } = {}): Promise<CommandResult> {
    const { remote, branch, setUpstream, ...rest } = options;
    if (setUpstream || branch) {
      return this.run(["push", ...(setUpstream ? ["--set-upstream"] : []), this.#ref(remote ?? "origin"), this.#ref(branch ?? "HEAD")], path, rest);
    }
    return this.run(["push", ...(remote ? [this.#ref(remote)] : [])], path, rest);
  }

  async pull(path: string, options: GitRunOptions & { remote?: string; branch?: string } = {}): Promise<CommandResult> {
    const { remote, branch, ...rest } = options;
    return this.run(["pull", ...(remote || branch ? [this.#ref(remote ?? "origin")] : []), ...(branch ? [this.#ref(branch)] : [])], path, rest);
  }

  async remoteAdd(path: string, name: string, url: string, options: GitOptions & { fetch?: boolean; overwrite?: boolean } = {}): Promise<CommandResult> {
    const { fetch, overwrite, ...rest } = options;
    const exists = overwrite && (await this.remoteGet(path, name, rest)) !== undefined;
    const result = await this.run(["remote", exists ? "set-url" : "add", name, url], path, rest);
    return fetch ? this.run(["fetch", name], path, rest) : result;
  }

  async remoteGet(path: string, name: string, options: GitOptions = {}): Promise<string | undefined> {
    const result = await this.run(["remote", "get-url", name], path, { ...options, check: false });
    return result.exitCode === 0 ? result.stdout.trim() : undefined;
  }

  /** `[name, url]` per remote. */
  async remotes(path: string, options: GitOptions = {}): Promise<[string, string][]> {
    const output = (await this.run(["remote", "-v"], path, options)).stdout;
    return output
      .split("\n")
      .filter((line) => line.endsWith("(fetch)"))
      .map((line) => line.split(/\s+/) as [string, string]);
  }

  async setConfig(key: string, value: string, options: GitOptions & { scope?: string; path?: string } = {}): Promise<CommandResult> {
    const { scope = "global", path, ...rest } = options;
    return this.run(["config", this.#scope(scope, path), key, value], path, rest);
  }

  async getConfig(key: string, options: GitOptions & { scope?: string; path?: string } = {}): Promise<string | undefined> {
    const { scope = "global", path, ...rest } = options;
    const result = await this.run(["config", this.#scope(scope, path), "--get", key], path, { ...rest, check: false });
    return result.exitCode === 0 ? result.stdout.trim() : undefined;
  }

  async configureUser(name: string, email: string, options: GitOptions & { scope?: string; path?: string } = {}): Promise<CommandResult> {
    await this.setConfig("user.name", name, options);
    return this.setConfig("user.email", email, options);
  }

  /** Store the credentials in plain text in the VM, for every later git command. */
  async dangerouslyAuthenticate(
    username: string,
    password: string,
    options: GitOptions & { host?: string; protocol?: string } = {},
  ): Promise<CommandResult> {
    const { host = "github.com", protocol = "https", ...rest } = options;
    await this.setConfig("credential.helper", "store", rest);
    const credential = `protocol=${protocol}\nhost=${host}\nusername=${username}\npassword=${password}\n`;
    const envs = { ...rest.envs, ARKER_GIT_CREDENTIAL: credential };
    const result = await this.sandbox.run(`printf %s "$ARKER_GIT_CREDENTIAL" | ${GIT} credential approve`, { cwd: rest.cwd ?? this.cwd, envs });
    if (result.exitCode !== 0) throw this.#fail(result);
    return result;
  }

  #scope(scope: string, path: string | undefined): string {
    if (!["global", "local", "system"].includes(scope)) {
      throw new this.#errors.invalid(`Git config scope must be global, local or system, not '${scope}'.`);
    }
    if (scope === "local" && !path) throw new this.#errors.invalid("A local git config scope needs the repository path.");
    return `--${scope}`;
  }
}

function withoutCredentials(options: GitRunOptions): GitRunOptions {
  const { username: _username, password: _password, ...rest } = options;
  return rest;
}

// Code contexts: stateful Python interpreters, one per Arker session, for
// every provider's code interpreter. A context's id is its session's id.

const INTERPRETER_READY = "<arker-interpreter-ready>";
// Marks an interpreter's session, and whether it is the sandbox's default context.
const INTERPRETER_ENV = "ARKER_INTERPRETER";
// The language of an interpreter's session; sessions without it run Python.
const INTERPRETER_LANGUAGE_ENV = "ARKER_INTERPRETER_LANGUAGE";

export type CodeLanguage = "python" | "javascript" | "typescript";

// Per language: the driver this package carries, where it goes in the VM, and how the VM runs it.
const INTERPRETERS: Record<CodeLanguage, { driver: string; path: string; command: string }> = {
  python: { driver: INTERPRETER, path: "/opt/arker-compat/interpreter.py", command: "python3 -u /opt/arker-compat/interpreter.py" },
  javascript: { driver: NODE_INTERPRETER, path: "/opt/arker-compat/interpreter.js", command: "node --no-warnings /opt/arker-compat/interpreter.js" },
  typescript: {
    driver: NODE_INTERPRETER,
    path: "/opt/arker-compat/interpreter.js",
    command: "node --no-warnings /opt/arker-compat/interpreter.js typescript",
  },
};

export interface CodeContext {
  id: string;
  cwd: string;
  default: boolean;
  language: CodeLanguage;
}

/** One displayed value: `data` maps a format (`text`, `html`, `png`...) to its content. */
export interface CodeResult {
  data: Record<string, any>;
  main: boolean;
}

export interface CodeExecution {
  results: CodeResult[];
  stdout: string;
  stderr: string;
  error?: { name: string; value: string; traceback: string };
  count: number;
}

/**
 * A sandbox's code contexts. Each keeps its variables and imports from one
 * run to the next until it is deleted or restarted, or a run times out.
 */
export class CodeContexts {
  readonly #defaults = new Map<CodeLanguage, string>();
  private readonly errors: Errors;

  constructor(private readonly sandbox: CompatSandbox) {
    this.errors = sandbox.errors;
  }

  async create(cwd?: string, options: { default?: boolean; language?: CodeLanguage } = {}): Promise<CodeContext> {
    const vm = this.sandbox.vm;
    const language = options.language ?? "python";
    const env = { [INTERPRETER_ENV]: options.default ? "default" : "context", [INTERPRETER_LANGUAGE_ENV]: language };
    const session = await translated(this.errors, () => vm.createSession({ env, ...(cwd ? { cwd } : {}) }));
    try {
      await this.#start(session.session_id, language);
    } catch (error) {
      await vm.deleteSession(session.session_id).catch(() => undefined);
      throw error;
    }
    return { id: session.session_id, cwd: session.cwd, default: Boolean(options.default), language };
  }

  async list(): Promise<CodeContext[]> {
    const contexts: CodeContext[] = [];
    for await (const session of paged(async (cursor) => {
      const page = await translated(this.errors, () => this.sandbox.vm.listSessions({ cursor }));
      return [page.sessions, page.next_cursor];
    })) {
      const kind = session.env?.[INTERPRETER_ENV];
      const language = (session.env?.[INTERPRETER_LANGUAGE_ENV] ?? "python") as CodeLanguage;
      if (kind) contexts.push({ id: session.session_id, cwd: session.cwd, default: kind === "default", language });
    }
    return contexts;
  }

  async delete(contextId: string): Promise<void> {
    await translated(this.errors, async () => {
      try {
        await this.sandbox.vm.deleteSession(contextId);
      } catch (error) {
        if (isMissing(error, 404)) throw new this.errors.notFound(`Code context ${contextId} not found`);
        throw error;
      }
    });
  }

  /** Start the context's interpreter afresh, forgetting its state. */
  async restart(contextId: string): Promise<void> {
    await this.#request(contextId, { restart: true });
  }

  /**
   * Run `code` in a context, by default the sandbox's default context for
   * `language`, created in `cwd` on first use. Errors in the code are part of
   * the result. Timeouts are in seconds.
   */
  async run(
    code: string,
    options: { contextId?: string; cwd?: string; envs?: Record<string, string>; timeout?: number; language?: CodeLanguage } = {},
  ): Promise<CodeExecution> {
    const contextId = options.contextId ?? (await this.#defaultContext(options.cwd, options.language ?? "python"));
    const envs = options.envs ?? {};
    const envsFile = Object.keys(envs).length > 0 ? await this.sandbox.privateFile(JSON.stringify(envs)) : null;
    return (await this.#request(contextId, { code, envs_file: envsFile }, options.timeout)) as CodeExecution;
  }

  async #defaultContext(cwd: string | undefined, language: CodeLanguage): Promise<string> {
    if (!this.#defaults.has(language)) {
      const existing = (await this.list()).find((context) => context.default && context.language === language)?.id;
      this.#defaults.set(language, existing ?? (await this.create(cwd, { default: true, language })).id);
    }
    return this.#defaults.get(language)!;
  }

  async #language(contextId: string): Promise<CodeLanguage> {
    return (await this.list()).find((context) => context.id === contextId)?.language ?? "python";
  }

  async #start(sessionId: string, language: CodeLanguage): Promise<void> {
    const { driver, path, command } = INTERPRETERS[language];
    await this.sandbox.write(path, driver);
    const script = `(\n[ -f ${ENV_FILE} ] && . ${ENV_FILE}\n${command}\n)`;
    const started = await translated(this.errors, () => this.sandbox.vm.run(script, { session_id: sessionId, end_symbol: INTERPRETER_READY }));
    if (!started.stdout.endsWith(INTERPRETER_READY)) {
      throw new this.errors.failed(`The code interpreter did not start: ${started.stderr || started.stdout}`);
    }
  }

  async #request(contextId: string, request: object, timeout?: number): Promise<Record<string, any>> {
    const line = `#${JSON.stringify(request)}`;
    let done: { stdout: string; stderr: string; exitCode: number | null } | undefined;
    for (let attempt = 0; attempt < 2; attempt++) {
      done = await translated(this.errors, async () => {
        try {
          return await this.sandbox.vm.run(line, { session_id: contextId, end_symbol: INTERPRETER_READY, timeout: killBound(timeout) });
        } catch (error) {
          if (isMissing(error, 404)) throw new this.errors.notFound(`Code context ${contextId} not found`);
          throw error;
        }
      });
      if (done.stdout.endsWith(INTERPRETER_READY)) {
        // A restart replies with the prompt alone.
        const reply = done.stdout.slice(0, -INTERPRETER_READY.length).trim();
        return reply ? JSON.parse(reply.split("\n").pop()!) : {};
      }
      if (timeout && done.exitCode === 124 && TIMED_OUT.test(done.stderr)) {
        throw new this.errors.timeout(`Code execution timed out after ${timeout}s; the context's state is lost.`);
      }
      // The interpreter is gone (it timed out, or the VM restarted without
      // its memory), and the session's shell read the request as a comment.
      if (attempt === 0) await this.#start(contextId, await this.#language(contextId));
    }
    throw new this.errors.failed(`The code interpreter is not responding: ${done!.stderr || done!.stdout}`);
  }
}

// Volumes: Arker filesystems, which persist apart from any VM and mount into many.

const MOUNT_WAIT_SECONDS = 120;

/** Named volumes, as Arker filesystems. Names are unique per organization. */
export class Volumes {
  readonly #arker: Arker;

  constructor(
    private readonly errors: Errors,
    config: CompatConfig = {},
  ) {
    const { source: _source, name: _name, ...arkerOptions } = config;
    this.#arker = new Arker(arkerOptions);
  }

  async list(): Promise<Filesystem[]> {
    const volumes: Filesystem[] = [];
    for await (const volume of this.#listed()) volumes.push(volume);
    return volumes;
  }

  async find(name: string): Promise<Filesystem | undefined> {
    for await (const volume of this.#listed(name)) if (volume.name === name) return volume;
    return undefined;
  }

  #listed(namePrefix?: string): AsyncGenerator<Filesystem> {
    return paged(async (cursor) => {
      const page = await translated(this.errors, () => this.#arker.listFilesystems({ cursor, ...(namePrefix ? { namePrefix } : {}) }));
      return [page.filesystems, page.next_cursor];
    });
  }

  async get(name: string, options: { create?: boolean } = {}): Promise<Filesystem> {
    const found = await this.find(name);
    if (found) return found;
    if (!options.create) throw new this.errors.notFound(`Volume '${name}' not found`);
    try {
      return await this.#arker.createFilesystem({ name });
    } catch (error) {
      // Another caller created it first.
      if (isMissing(error, 409)) return this.get(name);
      throw providerError(this.errors, error);
    }
  }

  async create(name: string): Promise<Filesystem> {
    try {
      return await this.#arker.createFilesystem({ name });
    } catch (error) {
      if (isMissing(error, 409)) throw new this.errors.invalid(`Volume '${name}' already exists`);
      throw providerError(this.errors, error);
    }
  }

  async byId(volumeId: string): Promise<Filesystem> {
    try {
      return await this.#arker.getFilesystem(volumeId);
    } catch (error) {
      if (isMissing(error, 400, 404)) throw new this.errors.notFound(`Volume ${volumeId} not found`);
      throw providerError(this.errors, error);
    }
  }

  /** Delete a volume and its data; false if there was no such volume. */
  async delete(volumeId: string): Promise<boolean> {
    try {
      await this.#arker.deleteFilesystem(volumeId);
      return true;
    } catch (error) {
      if (isMissing(error, 404)) return false;
      throw providerError(this.errors, error);
    }
  }
}

/**
 * An interactive shell attached over a WebSocket; see {@link CompatSandbox.openTerminal}.
 *
 * Output is delivered to `onData` and to {@link Terminal.output}. Detaching
 * leaves the shell running; {@link Terminal.kill} ends it.
 */
export class Terminal {
  readonly #chunks: (Uint8Array | null)[] = [];
  #wake: (() => void) | undefined;
  readonly #closed: Promise<void>;

  private constructor(
    private readonly sandbox: CompatSandbox,
    readonly sessionId: string,
    readonly pid: number,
    private readonly connection: PtyConnection,
    onData: ((data: Uint8Array) => void | Promise<void>) | undefined,
  ) {
    connection.onData((data) => {
      void onData?.(data);
      this.#push(data);
    });
    this.#closed = new Promise((resolve) => connection.onClose(() => resolve()));
    void this.#closed.then(() => this.#push(null));
  }

  /** @internal */
  static async attach(
    sandbox: CompatSandbox,
    sessionId: string,
    pid: number,
    options: { cols?: number; rows?: number; onData?: (data: Uint8Array) => void | Promise<void> },
  ): Promise<Terminal> {
    return translated(sandbox.errors, async () => {
      const connection = await sandbox.vm.connectPty({ sessionId, cols: options.cols, rows: options.rows });
      const terminal = new Terminal(sandbox, sessionId, pid, connection, options.onData);
      await connection.ready;
      return terminal;
    });
  }

  #push(chunk: Uint8Array | null): void {
    this.#chunks.push(chunk);
    this.#wake?.();
  }

  /** Terminal output as it arrives, until the shell exits or is detached. */
  async *output(): AsyncGenerator<Uint8Array> {
    for (;;) {
      const chunk = this.#chunks.shift();
      if (chunk === null) return;
      if (chunk) {
        yield chunk;
        continue;
      }
      await new Promise<void>((resolve) => (this.#wake = resolve));
    }
  }

  send(data: string | Uint8Array): void {
    this.connection.send(data);
  }

  async resize(cols: number, rows: number): Promise<void> {
    await this.sandbox.resizeTerminal(this.sessionId, cols, rows);
  }

  /** Wait for the shell to exit; a killed shell is an error. */
  async wait(): Promise<CommandResult> {
    await this.#closed;
    const exitCode = await this.sandbox.terminalExitCode(this.sessionId);
    if (exitCode === undefined) throw new this.sandbox.errors.failed("The terminal was killed.");
    return { stdout: "", stderr: "", exitCode };
  }

  detach(): void {
    this.connection.close();
  }

  async kill(): Promise<boolean> {
    return this.sandbox.killSession(this.sessionId);
  }
}
