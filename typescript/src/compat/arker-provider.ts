import type {
  CommandResult,
  CreateSandboxOptions,
  FileEntry,
  RunCommandOptions,
  SandboxFileSystem,
  SandboxInfo,
  SandboxInterface,
  StartProcessOptions,
  ProcessHandle,
} from "computesdk";
import { randomUUID } from "node:crypto";

import {
  Arker,
  ArkerError,
  type ArkerOptions,
  type ForkOptions,
  type RunOptions,
  type VM,
} from "../index.js";
import { emitFinalOutput, formatEnvExports, shellQuote } from "./common.js";

export interface ArkerComputeProviderConfig extends ArkerOptions {
  /** Source VM name. Retained for compatibility. */
  source?: string;
  sourceVmName?: string;
  sourceVmId?: string;
  /** Applied at fork time; policy secrets stay outside the guest environment. */
  policies?: ForkOptions["policies"];
  /** Used only when creating from an image, never when forking an existing VM. */
  registryAuth?: ForkOptions["registry_auth"];
}

interface DirectProvider {
  readonly name: string;
  readonly sandbox: {
    create(options?: CreateSandboxOptions): Promise<SandboxInterface>;
    getById(sandboxId: string): Promise<SandboxInterface | null>;
    list(): Promise<SandboxInterface[]>;
    destroy(sandboxId: string): Promise<void>;
  };
}

function env(key: string): string | undefined {
  const value = globalThis.process?.env?.[key];
  const trimmed = value?.trim();
  return trimmed ? trimmed : undefined;
}

function makeClient(config: ArkerComputeProviderConfig = {}): Arker {
  return new Arker(config);
}

function decode(bytes: Uint8Array): string {
  return new TextDecoder().decode(bytes);
}

function splitList(value: unknown): string[] {
  if (typeof value === "string") return value.split(",").map((entry) => entry.trim()).filter(Boolean);
  if (Array.isArray(value) && value.every((entry) => typeof entry === "string")) return value;
  return [];
}

function networkPolicies(values: Record<string, unknown>): ForkOptions["policies"] | undefined {
  const blockAll = values.allowInternetAccess === false || values.networkBlockAll === true || values.blockNetwork === true;
  const domains = splitList(values.domainAllowList ?? values.outboundDomainAllowlist);
  const cidrs = splitList(values.networkAllowList ?? values.outboundCidrAllowlist);
  if (blockAll && (domains.length > 0 || cidrs.length > 0)) {
    throw new ArkerError("bad_request", "block-all network policy cannot be combined with outbound allowlists", 400);
  }
  if (!blockAll && domains.length === 0 && cidrs.length === 0) return undefined;
  const policies: Array<Record<string, unknown>> = [];
  if (domains.length > 0) {
    const modalDomainPolicy = values.outboundDomainAllowlist !== undefined;
    policies.push({
      type: "outbound",
      match: { hosts: domains, ...(modalDomainPolicy ? { ports: [443] } : {}) },
      action: "allow",
    });
  }
  if (cidrs.length > 0) {
    policies.push({ type: "outbound", match: { ips: cidrs }, action: "allow" });
  }
  policies.push({ type: "outbound", action: "deny" });
  // Fork policy updates normally merge with the source VM. The ordered form
  // replaces inherited rules so provider allowlists cannot be bypassed.
  return { policies } as ForkOptions["policies"];
}

function toFileEntries(stdout: string): FileEntry[] {
  const entries: FileEntry[] = [];
  // POSIX filenames may contain tabs and newlines, but never NUL.
  const fields = stdout.split("\0");
  for (let index = 0; index + 3 < fields.length; index += 4) {
    const [type, size, mtime, name] = fields.slice(index, index + 4);
    entries.push({
      name: name!,
      type: type === "d" ? "directory" : "file",
      size: Number(size) || 0,
      modified: new Date((Number(mtime) || 0) * 1000),
    });
  }
  return entries;
}

class ArkerComputeSandbox implements SandboxInterface {
  readonly instanceId: string;
  readonly sandboxId: string;
  readonly provider = "arker";
  readonly filesystem: SandboxFileSystem;

  constructor(
    private readonly vm: VM,
    private readonly destroyVm: (sandboxId: string) => Promise<void>,
  ) {
    this.sandboxId = vm.id;
    this.instanceId = vm.id;
    this.filesystem = {
      readFile: async (path) => decode(await this.vm.sync(path)),
      writeFile: async (path, content) => {
        await this.vm.sync(path, content);
      },
      mkdir: async (path) => {
        const result = await this.runCommand(`mkdir -p -- ${shellQuote(path)}`);
        if (result.exitCode !== 0) throw new Error(`Arker mkdir failed for ${path}: ${result.stderr || `exit ${result.exitCode}`}`);
      },
      readdir: async (path) => {
        const p = shellQuote(path);
        const script =
          `cd -- ${p} 2>/dev/null || exit 3; ` +
          `for e in * .*; do ` +
          `[ "$e" = "." ] && continue; [ "$e" = ".." ] && continue; [ -e "$e" ] || [ -L "$e" ] || continue; ` +
          `if [ -d "$e" ]; then t=d; else t=f; fi; ` +
          `s=$(stat -c %s -- "$e" 2>/dev/null || echo 0); m=$(stat -c %Y -- "$e" 2>/dev/null || echo 0); ` +
          `printf '%s\\000%s\\000%s\\000%s\\000' "$t" "$s" "$m" "$e"; done`;
        const result = await this.runCommand(script);
        if (result.exitCode !== 0) throw new Error(`Arker readdir failed for ${path}: ${result.stderr || `exit ${result.exitCode}`}`);
        return toFileEntries(result.stdout);
      },
      exists: async (path) => {
        const result = await this.runCommand(`test -e ${shellQuote(path)}`);
        return result.exitCode === 0;
      },
      remove: async (path) => {
        const result = await this.runCommand(`rm -rf -- ${shellQuote(path)}`);
        if (result.exitCode !== 0) throw new Error(`Arker remove failed for ${path}: ${result.stderr || `exit ${result.exitCode}`}`);
      },
    };
  }

  getInstance(): VM {
    return this.vm;
  }

  async runCommand(command: string, options?: RunCommandOptions): Promise<CommandResult> {
    const startTime = Date.now();
    let fullCommand = command;

    if (options?.env && Object.keys(options.env).length > 0) {
      const envPrefix = formatEnvExports(options.env);
      fullCommand = `${envPrefix} ${fullCommand}`;
    }
    if (options?.cwd) fullCommand = `cd ${shellQuote(options.cwd)} && ${fullCommand}`;
    if (options?.background) fullCommand = `nohup sh -lc ${shellQuote(fullCommand)} > /dev/null 2>&1 &`;

    const runOptions: RunOptions = {};
    if (options?.timeout !== undefined) {
      runOptions.timeout = options.timeout === 0 ? 0 : Math.ceil(options.timeout / 1_000);
    }

    // Vendor exec calls start a fresh shell; Arker's default session persists.
    const result = await this.vm.run(`(\n${fullCommand}\n)`, runOptions);
    if (result.type !== "completed") {
      throw new Error(`Arker run did not complete synchronously (type=${result.type}). runCommand requires a foreground command.`);
    }

    const commandResult = {
      stdout: result.stdout,
      stderr: result.stderr,
      exitCode: result.exitCode,
      durationMs: Date.now() - startTime,
    };
    emitFinalOutput(commandResult, options);
    return commandResult;
  }

  async startProcess(command: string, options?: StartProcessOptions): Promise<ProcessHandle> {
    if (options?.stdin) {
      throw new ArkerError(
        "unsupported_operation",
        "Writable process stdin requires an Arker PTY session and is not available through this adapter",
        422,
      );
    }
    let fullCommand = command;
    if (options?.env && Object.keys(options.env).length > 0) {
      const envPrefix = formatEnvExports(options.env);
      fullCommand = `${envPrefix} ${fullCommand}`;
    }
    if (options?.cwd) fullCommand = `cd ${shellQuote(options.cwd)} && ${fullCommand}`;
    const jobId = randomUUID();
    const jobDirectory = `/tmp/arker-computesdk/${jobId}`;
    const stderrMarker = `__ARKER_STDERR_${jobId}__`;
    const pidPath = `${jobDirectory}/pid`;
    const launcher =
      `mkdir -p ${shellQuote(jobDirectory)}; ` +
      `nohup setsid --fork --wait sh -lc ${shellQuote(`printf '%s' "$$" > ${shellQuote(pidPath)}; awk '{print $22}' /proc/$$/stat > ${shellQuote(`${jobDirectory}/start`)}; ( ${fullCommand}\n); code=$?; printf '%s' "$code" > ${shellQuote(`${jobDirectory}/exit`)}`)} ` +
      `> ${shellQuote(`${jobDirectory}/stdout`)} 2> ${shellQuote(`${jobDirectory}/stderr`)} < /dev/null & ` +
      `launcher=$!; while [ ! -s ${shellQuote(pidPath)} ] && kill -0 "$launcher" 2>/dev/null; do sleep 0.01; done; ` +
      `test -s ${shellQuote(pidPath)}`;
    const launched = await this.runCommand(launcher);
    if (launched.exitCode !== 0) throw new Error(`Failed to start Arker process: ${launched.stderr || `exit ${launched.exitCode}`}`);

    const snapshot = async () => {
      const result = await this.runCommand(
        `d=${shellQuote(jobDirectory)}; ` +
        `pid=$(cat "$d/pid" 2>/dev/null || true); ` +
        `signal=$(cat "$d/signal" 2>/dev/null || true); ` +
        `start=$(cat "$d/start" 2>/dev/null || true); current=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true); ` +
        `if [ -f "$d/exit" ]; then state=exited; code=$(cat "$d/exit"); ` +
        `elif case "$pid:$start:$current" in *[!0-9:]*|:*|*::*|*:) false;; *) [ "$start" = "$current" ] && kill -0 "$pid" 2>/dev/null;; esac; then state=running; code=; ` +
        `else state=exited; code=-1; printf '%s' "$code" > "$d/exit"; fi; ` +
        `printf '%s\\n%s\\n%s\\n' "$state" "$code" "$signal"; cat "$d/stdout" 2>/dev/null; printf '\\n${stderrMarker}\\n'; cat "$d/stderr" 2>/dev/null`,
      );
      const [header, stderr = ""] = result.stdout.split(`\n${stderrMarker}\n`, 2);
      const [state = "running", code = "", signal = "", ...stdoutLines] = header.split("\n");
      const running = state === "running";
      return {
        status: running ? "running" as const : "exited" as const,
        exitCode: running ? null : Number.parseInt(code, 10),
        signal: signal || null,
        stdout: stdoutLines.join("\n"),
        stderr,
      };
    };

    return {
      pid: null,
      jobId,
      write: async () => { throw new Error("Process stdin was not enabled"); },
      closeStdin: async () => undefined,
      status: snapshot,
      wait: async (waitOptions) => {
        const deadline = waitOptions?.timeout === undefined ? undefined : Date.now() + waitOptions.timeout;
        while (true) {
          const state = await snapshot();
          if (state.status === "exited") {
            options?.onStdout?.(state.stdout);
            options?.onStderr?.(state.stderr);
            options?.onExit?.({ exitCode: state.exitCode, signal: state.signal });
            return { ...state, exitCode: state.exitCode ?? -1, durationMs: 0 };
          }
          if (deadline !== undefined && Date.now() >= deadline) throw new Error(`daemond: wait timed out for ${jobId}`);
          await new Promise((resolve) => setTimeout(resolve, options?.pollIntervalMs ?? 500));
        }
      },
      kill: async (requestedSignal = "SIGTERM") => {
        const signal = requestedSignal.toUpperCase().startsWith("SIG")
          ? requestedSignal.toUpperCase()
          : `SIG${requestedSignal.toUpperCase()}`;
        const signalNumber = ({
          SIGHUP: 1, SIGINT: 2, SIGQUIT: 3, SIGKILL: 9, SIGTERM: 15,
          SIGUSR1: 10, SIGUSR2: 12,
        } as Record<string, number>)[signal];
        if (signalNumber === undefined) throw new Error(`Invalid process signal ${requestedSignal}`);
        const exitCode = 128 + signalNumber;
        const result = await this.runCommand(
          `d=${shellQuote(jobDirectory)}; [ -f "$d/exit" ] && exit 0; pid=$(cat "$d/pid"); ` +
          `start=$(cat "$d/start" 2>/dev/null || true); current=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true); ` +
          `case "$pid:$start:$current" in *[!0-9:]*|:*|*::*|*:) exit 4;; esac; ` +
          `[ "$start" = "$current" ] || { printf '%s' -1 > "$d/exit"; exit 0; }; ` +
          `/bin/kill -s ${shellQuote(signal.slice(3))} -- "-$pid" 2>/dev/null ` +
          `|| /bin/kill -s ${shellQuote(signal.slice(3))} "$pid" 2>/dev/null ` +
          `|| { kill -0 "$pid" 2>/dev/null && exit 5 || true; }; ` +
          `printf '%s' ${shellQuote(signal)} > "$d/signal"; printf '%s' ${exitCode} > "$d/exit"`,
        );
        if (result.exitCode !== 0) throw new Error(`Failed to kill Arker process: ${result.stderr || `exit ${result.exitCode}`}`);
      },
    };
  }

  async getInfo(): Promise<SandboxInfo> {
    return {
      id: this.sandboxId,
      provider: "arker",
      status: "running",
      createdAt: this.vm.created_at ? new Date(this.vm.created_at) : new Date(),
      timeout: 0,
      metadata: {
        name: this.vm.name,
        hostname: this.vm.hostname,
        region: this.vm.region,
        provider: this.vm.provider,
      },
    };
  }

  async getUrl(options: { port: number; protocol?: string }): Promise<string> {
    if (options.protocol !== undefined && options.protocol !== "https") {
      throw new ArkerError("unsupported_operation", "Arker inbound services use HTTPS", 422);
    }
    if (!Number.isInteger(options.port) || options.port < 1 || options.port > 65_535) {
      throw new ArkerError("invalid_request", "port must be an integer from 1 through 65535", 422);
    }
    const vm = this.vm.hostname ? this.vm : await this.vm.refresh();
    if (!vm.hostname) {
      throw new ArkerError("unsupported_operation", "Arker did not return an inbound hostname for this VM", 422);
    }
    const hostname = options.port === 8080
      ? vm.hostname
      : vm.hostname.replace(/^([^.]+)/, `$1-${options.port}`);
    return `https://${hostname}`;
  }

  async destroy(): Promise<void> {
    await this.destroyVm(this.sandboxId);
  }
}

export function createArkerComputeProvider(config: ArkerComputeProviderConfig = {}): DirectProvider {
  async function destroy(sandboxId: string): Promise<void> {
    const client = makeClient(config);
    try {
      await client.vm(sandboxId).delete();
    } catch (error) {
      if (error instanceof ArkerError && error.status === 404) return;
      throw error;
    }
  }

  function wrap(vm: VM): SandboxInterface {
    return new ArkerComputeSandbox(vm, destroy);
  }

  return {
    name: "arker",
    sandbox: {
      create: async (options?: CreateSandboxOptions): Promise<SandboxInterface> => {
        const client = makeClient(config);
        const values = (options ?? {}) as Record<string, unknown>;
        const supported = new Set([
          "name", "image", "memory", "memoryMiB", "cpu", "vcpus", "diskMiB", "snapshotId", "templateId",
          "networkPolicy", "networkPolicies", "internetAccess", "autoStopInterval", "allowInternetAccess",
          "networkBlockAll", "blockNetwork", "domainAllowList", "outboundDomainAllowlist", "networkAllowList",
          "outboundCidrAllowlist", "createIfNotExist",
        ]);
        const unsupported = Object.keys(values).filter((key) => !supported.has(key) && values[key] !== undefined);
        if (values.createIfNotExist !== undefined && values.createIfNotExist !== false) {
          unsupported.push("createIfNotExist");
        }
        if (unsupported.length > 0) {
          throw new ArkerError(
            "unsupported_operation",
            `Arker create does not support option(s): ${unsupported.join(", ")}`,
            422,
          );
        }
        const sourceVmId = options?.snapshotId || config.sourceVmId || env("ARKER_SOURCE_VM");
        const sourceVmName = options?.templateId || config.sourceVmName || config.source || env("ARKER_SOURCE");
        if (values.image !== undefined && (typeof values.image !== "string" || !values.image.trim())) {
          throw new ArkerError("unsupported_operation", "Arker image must be a published image reference, not a provider-specific image builder", 422);
        }
        const image = typeof values.image === "string" ? values.image : undefined;
        if (!sourceVmId && !sourceVmName && !image) {
          throw new Error("Arker source is required; pass templateId, snapshotId, image, configure source, or set ARKER_SOURCE");
        }
        const vcpu = typeof values.cpu === "number" ? values.cpu : typeof values.vcpus === "number" ? values.vcpus : undefined;
        const memoryMiB = typeof values.memoryMiB === "number" ? values.memoryMiB : typeof values.memory === "number" ? values.memory : undefined;
        const diskMiB = typeof values.diskMiB === "number" ? values.diskMiB : undefined;
        const resources = vcpu !== undefined || memoryMiB !== undefined || diskMiB !== undefined
          ? { ...(vcpu !== undefined ? { vcpu } : {}), ...(memoryMiB !== undefined ? { memory_mib: memoryMiB } : {}), ...(diskMiB !== undefined ? { disk_mib: diskMiB } : {}) }
          : undefined;
        const sourceRequest: ForkOptions = image
          ? { image }
          : sourceVmId
            ? { source_vm_id: sourceVmId }
            : { source_vm_name: sourceVmName! };
        const network = networkPolicies(values);
        const policies = config.policies || network ? { ...config.policies, ...network } : undefined;
        if (network && policies) delete policies.rules;
        const request: ForkOptions = {
          ...sourceRequest,
          ...(options?.name ? { name: options.name } : {}),
          ...(resources ? { resources } : {}),
          ...(policies ? { policies } : {}),
          ...(image && config.registryAuth ? { registry_auth: config.registryAuth } : {}),
        };
        const vm = await client.fork(request);
        return wrap(vm);
      },
      getById: async (sandboxId: string): Promise<SandboxInterface | null> => {
        const client = makeClient(config);
        try {
          return wrap(await client.getVm(sandboxId));
        } catch (error) {
          if (error instanceof ArkerError && error.status === 404) return null;
          throw error;
        }
      },
      list: async (): Promise<SandboxInterface[]> => {
        const client = makeClient(config);
        const { vms } = await client.listVms();
        return (vms ?? []).map(wrap);
      },
      destroy,
    },
  };
}
