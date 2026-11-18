/**
 * E2B Code Interpreter-compatible sandboxes backed by Arker VMs.
 *
 * Replace `from "@e2b/code-interpreter"` with
 * `from "@arker-ai/sdk/e2b-code-interpreter"`. Code runs in a stateful Python,
 * JavaScript or TypeScript interpreter per context, so variables and imports
 * carry over between runs.
 * Output reaches the callbacks once a run finishes, not while it runs.
 */
import { CodeContexts, unsupported, type CodeContext, type CodeExecution, type CodeLanguage, type CompatSandbox } from "./compat/core.js";
import { ERRORS, InvalidArgumentError, Sandbox as E2BSandbox, type SandboxConnectOpts, type SandboxOpts } from "./e2b.js";

export * from "./e2b.js";

const HOME = "/home/user";
const DEFAULT_TIMEOUT_MS = 300_000;
// E2B's language names, and the core's.
const LANGUAGES: Record<string, CodeLanguage> = {
  python: "python",
  js: "javascript",
  javascript: "javascript",
  ts: "typescript",
  typescript: "typescript",
};
// What a Result carries, beyond text: the formats Jupyter displays.
const FORMATS = ["html", "markdown", "svg", "png", "jpeg", "pdf", "latex", "json", "javascript"] as const;

export class OutputMessage {
  constructor(
    readonly line: string,
    readonly timestamp: number,
    readonly error: boolean,
  ) {}

  toString(): string {
    return this.line;
  }
}

export class ExecutionError {
  constructor(
    public name: string,
    public value: string,
    public traceback: string,
  ) {}
}

export type RawData = Record<string, any>;

/** A value the code displayed: the cell's last expression (`isMainResult`), or a chart. */
export class Result {
  readonly text?: string;
  readonly html?: string;
  readonly markdown?: string;
  readonly svg?: string;
  readonly png?: string;
  readonly jpeg?: string;
  readonly pdf?: string;
  readonly latex?: string;
  readonly json?: string;
  readonly javascript?: string;
  readonly data?: Record<string, unknown>;
  readonly chart?: unknown;
  readonly extra?: any;

  constructor(
    readonly raw: RawData,
    readonly isMainResult: boolean,
  ) {
    Object.assign(this, Object.fromEntries(["text", ...FORMATS].filter((name) => raw[name] !== undefined).map((name) => [name, raw[name]])));
  }

  formats(): string[] {
    return FORMATS.filter((name) => this[name] !== undefined);
  }

  toJSON() {
    return Object.fromEntries(["text", ...FORMATS].map((name) => [name, this[name as keyof Result]]));
  }
}

export interface Logs {
  stdout: string[];
  stderr: string[];
}

export class Execution {
  constructor(
    public results: Result[] = [],
    public logs: Logs = { stdout: [], stderr: [] },
    public error?: ExecutionError,
    public executionCount?: number,
  ) {}

  get text(): string | undefined {
    return this.results.find((result) => result.isMainResult)?.text;
  }

  toJSON() {
    return { results: this.results, logs: this.logs, error: this.error };
  }
}

export type Context = { id: string; language: string; cwd: string };
export type RunCodeLanguage = "python" | "javascript" | "typescript" | "r" | "java" | "bash" | (string & {});

export interface RunCodeOpts {
  onStdout?: (output: OutputMessage) => Promise<any> | any;
  onStderr?: (output: OutputMessage) => Promise<any> | any;
  onResult?: (data: Result) => Promise<any> | any;
  onError?: (error: ExecutionError) => Promise<any> | any;
  envs?: Record<string, string>;
  timeoutMs?: number;
  requestTimeoutMs?: number;
}

export interface CreateCodeContextOpts {
  cwd?: string;
  language?: RunCodeLanguage;
  requestTimeoutMs?: number;
}

const context = (found: CodeContext): Context => ({ id: found.id, language: found.language, cwd: found.cwd });
const contextId = (found: Context | string): string => (typeof found === "string" ? found : found.id);

function languageOf(language: string | undefined): CodeLanguage {
  if (language === undefined) return "python";
  const known = Object.hasOwn(LANGUAGES, language) ? LANGUAGES[language] : undefined;
  if (!known) throw unsupported(ERRORS, `runCode({ language: '${language}' })`, "run other languages with commands.run.");
  return known;
}

function execution(done: CodeExecution): Execution {
  return new Execution(
    done.results.map((result) => new Result(result.data, result.main)),
    { stdout: done.stdout ? [done.stdout] : [], stderr: done.stderr ? [done.stderr] : [] },
    done.error ? new ExecutionError(done.error.name, done.error.value, done.error.traceback) : undefined,
    done.count,
  );
}

/** An E2B code interpreter sandbox running as an Arker VM. */
export class Sandbox extends E2BSandbox {
  // Not #private: the base constructor returns a proxy, and #private members would live on it, not on the sandbox its methods run on.
  private contexts?: CodeContexts;

  /** @internal */
  protected constructor(sandbox: CompatSandbox) {
    super(sandbox);
  }

  static override async create(opts?: SandboxOpts): Promise<Sandbox>;
  static override async create(template: string, opts?: SandboxOpts): Promise<Sandbox>;
  static override async create(templateOrOpts?: string | SandboxOpts, opts?: SandboxOpts): Promise<Sandbox> {
    const create = E2BSandbox.create as (templateOrOpts?: string | SandboxOpts, opts?: SandboxOpts) => Promise<E2BSandbox>;
    return (await create.call(this, templateOrOpts, opts)) as Sandbox;
  }

  static override async connect(sandboxId: string, opts?: SandboxConnectOpts): Promise<Sandbox> {
    return (await E2BSandbox.connect.call(this, sandboxId, opts)) as Sandbox;
  }

  private interpreter(): CodeContexts {
    this.contexts ??= new CodeContexts(this.compat);
    return this.contexts;
  }

  /**
   * Run `code` in Python, or in JavaScript or TypeScript (`"js"`, `"ts"`) on Node; an
   * exception it raises is `Execution.error`, not thrown here.
   */
  async runCode(code: string, opts: RunCodeOpts & { language?: RunCodeLanguage; context?: Context } = {}): Promise<Execution> {
    const { language, context: target, onStdout, onStderr, onResult, onError, envs, timeoutMs } = opts;
    if (language && target) throw new InvalidArgumentError("You can provide context or language, but not both at the same time.");
    const timeout = timeoutMs === 0 ? undefined : (timeoutMs ?? DEFAULT_TIMEOUT_MS) / 1_000;
    const run = { contextId: target?.id, cwd: HOME, envs, timeout, language: languageOf(language) };
    const done = execution(await this.interpreter().run(code, run));
    const now = Date.now();
    for (const line of done.logs.stdout) await onStdout?.(new OutputMessage(line, now, false));
    for (const line of done.logs.stderr) await onStderr?.(new OutputMessage(line, now, true));
    for (const result of done.results) await onResult?.(result);
    if (done.error) await onError?.(done.error);
    return done;
  }

  async createCodeContext(opts: CreateCodeContextOpts = {}): Promise<Context> {
    return context(await this.interpreter().create(opts.cwd ?? HOME, { language: languageOf(opts.language) }));
  }

  async listCodeContexts(): Promise<Context[]> {
    return (await this.interpreter().list()).map(context);
  }

  async removeCodeContext(target: Context | string): Promise<void> {
    await this.interpreter().delete(contextId(target));
  }

  async restartCodeContext(target: Context | string): Promise<void> {
    await this.interpreter().restart(contextId(target));
  }
}

export default Sandbox;
