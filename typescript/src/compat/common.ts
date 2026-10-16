import type { CommandResult, RunCommandOptions, SandboxInterface } from "computesdk";

export type UniversalSandbox = SandboxInterface;

export interface CreateOptions {
  timeout?: number;
  timeoutMs?: number;
  routerConfigPath?: string;
  envs?: Record<string, string>;
  metadata?: Record<string, unknown>;
}

export function shouldGuardNested(value: unknown): value is object {
  return !!value && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype;
}

export function guardUnsupported<T extends object>(instance: T, shimName: string): T {
  const passthrough = new Set<PropertyKey>([
    "then",
    "catch",
    "finally",
    "constructor",
    "toJSON",
    "toString",
    "valueOf",
    Symbol.toStringTag,
  ]);
  const supported = new Set<PropertyKey>();
  for (let current: object | null = instance; current && current !== Object.prototype; current = Object.getPrototypeOf(current)) {
    for (const key of Reflect.ownKeys(current)) supported.add(key);
  }

  return new Proxy(instance, {
    get(target, prop) {
      if (typeof prop === "symbol" || passthrough.has(prop) || supported.has(prop)) {
        const value = Reflect.get(target, prop, target);
        if (typeof value === "function") {
          return (value as (...args: unknown[]) => unknown).bind(target);
        }
        return shouldGuardNested(value) ? guardUnsupported(value, shimName) : value;
      }

      throw new Error(
        `'${String(prop)}' is not supported by the Arker compatibility layer for ${shimName}. ` +
          `The compatibility layer covers create/connect/get, command execution, and core filesystem operations. ` +
          `Use the native ${shimName} SDK directly for the full API.`,
      );
    },
  });
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function formatKeys(keys: string[]): string {
  return keys.map((key) => `'${key}'`).join(", ");
}

export function assertNoUnsupportedArgs(args: readonly unknown[], shimName: string, methodName: string): void {
  if (args.length === 0) return;
  throw new Error(
    `${methodName} received unsupported positional argument(s) in the Arker compatibility layer for ${shimName}. ` +
      `Those values would be ignored, so this layer fails fast. Use the native ${shimName} SDK for app/image/provider-specific options.`,
  );
}

export function assertSupportedObjectKeys(
  value: unknown,
  supportedKeys: readonly string[],
  shimName: string,
  methodName: string,
): void {
  if (value === undefined) return;
  if (!isPlainRecord(value)) {
    throw new Error(
      `${methodName} only accepts an options object in the Arker compatibility layer for ${shimName}. ` +
        `Use the native ${shimName} SDK for unsupported call signatures.`,
    );
  }

  const supported = new Set(supportedKeys);
  const unsupported = Object.keys(value).filter((key) => !supported.has(key));
  if (unsupported.length > 0) {
    throw new Error(
      `${methodName} received unsupported option(s) ${formatKeys(unsupported)} in the Arker compatibility layer for ${shimName}. ` +
        `Those options would be ignored, so this layer fails fast. Supported option(s): ` +
        `${supportedKeys.length > 0 ? formatKeys([...supportedKeys]) : "(none)"}.`,
    );
  }
}

export function resolveTimeout(opts?: CreateOptions): number | undefined {
  if (opts?.timeout !== undefined && opts.timeoutMs !== undefined && opts.timeout !== opts.timeoutMs) {
    throw new Error("Received both 'timeout' and 'timeoutMs' with different values. Use one timeout value.");
  }
  return opts?.timeoutMs ?? opts?.timeout;
}

export function isStringRecord(value: unknown): value is Record<string, string> {
  return !!value && typeof value === "object" && !Array.isArray(value) &&
    Object.values(value).every((entry) => typeof entry === "string");
}

export function shellQuote(value: string): string {
  if (value === "") return "''";
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

export function resolveCompatPath(path: string, defaultDirectory: string): string {
  if (path.startsWith("/")) return path;
  const parts = `${defaultDirectory}/${path}`.split("/");
  const resolved: string[] = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") resolved.pop();
    else resolved.push(part);
  }
  return `/${resolved.join("/")}`;
}

export function formatEnvExports(env: Record<string, string>): string {
  return Object.entries(env).map(([key, value]) => {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new Error(`Invalid environment variable name: ${key}`);
    return `export ${key}=${shellQuote(value)};`;
  }).join(" ");
}

export function commandName(command: string): string {
  return command.split("/").pop() || command;
}

export function argvToCommand(cmd: string[] | string): string {
  if (typeof cmd === "string") return cmd;
  const executable = commandName(cmd[0] ?? "");
  if ((executable === "sh" || executable === "bash") && cmd.length >= 3 && (cmd[1] === "-c" || cmd[1] === "-lc")) {
    return cmd[2]!;
  }
  return cmd.map(shellQuote).join(" ");
}

export function emitFinalOutput(result: CommandResult, options?: RunCommandOptions): void {
  if (result.stdout && options?.onStdout) options.onStdout(result.stdout);
  if (result.stderr && options?.onStderr) options.onStderr(result.stderr);
}
