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
  for (const line of stdout.split("\n")) {
    if (!line) continue;
    const parts = line.split("\t");
    if (parts.length < 4) continue;
    const [type, size, mtime, ...nameParts] = parts;
    entries.push({
      name: nameParts.join("\t"),
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
        const result = await this.runCommand(`mkdir -p ${shellQuote(path)}`);
        if (result.exitCode !== 0) throw new Error(`Arker mkdir failed for ${path}: ${result.stderr || `exit ${result.exitCode}`}`);
      },
      readdir: async (path) => {
        const p = shellQuote(path);
        const script =
          `cd ${p} 2>/dev/null || exit 3; ` +
          `for e in * .*; do ` +
          `[ "$e" = "." ] && continue; [ "$e" = ".." ] && continue; [ -e "$e" ] || continue; ` +
          `if [ -d "$e" ]; then t=d; else t=f; fi; ` +
          `s=$(stat -c %s "$e" 2>/dev/null || echo 0); m=$(stat -c %Y "$e" 2>/dev/null || echo 0); ` +
          `printf '%s\\t%s\\t%s\\t%s\\n' "$t" "$s" "$m" "$e"; done`;
        const result = await this.runCommand(script);
        if (result.exitCode !== 0) throw new Error(`Arker readdir failed for ${path}: ${result.stderr || `exit ${result.exitCode}`}`);
        return toFileEntries(result.stdout);
      },
      exists: async (path) => {
        const result = await this.runCommand(`test -e ${shellQuote(path)}`);
        return result.exitCode === 0;
      },
      remove: async (path) => {
        const result = await this.runCommand(`rm -rf ${shellQuote(path)}`);
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

    const result = await this.vm.run(fullCommand, runOptions);
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
    const background = await this.vm.run(fullCommand, { time_to_background: 0 });
    const runId = background.runId;

    const snapshot = async () => {
      const run = await this.vm.getRun(runId) as any;
      const running = run.state === "running" || run.status === "running";
      return {
        status: running ? "running" as const : "exited" as const,
        exitCode: running ? null : (run.exitCode ?? run.exit_code ?? null),
        signal: null,
        stdout: run.stdout ?? "",
        stderr: run.stderr ?? "",
      };
    };

    return {
      pid: null,
      jobId: runId,
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
            options?.onExit?.({ exitCode: state.exitCode, signal: null });
            return { ...state, exitCode: state.exitCode ?? -1, durationMs: 0 };
          }
          if (deadline !== undefined && Date.now() >= deadline) throw new Error(`daemond: wait timed out for ${runId}`);
          await new Promise((resolve) => setTimeout(resolve, options?.pollIntervalMs ?? 500));
        }
      },
      kill: async () => { await this.vm.cancelRun(runId); },
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
        region: this.vm.region,
        provider: this.vm.provider,
      },
    };
  }

  async getUrl(_options: { port: number; protocol?: string }): Promise<string> {
    // arkerd does not expose tunnel/ingress URLs; the public VM API has no
    // tunnels surface. Inbound reachability is configured via the VM network
    // settings instead.
    throw new ArkerError(
      "unsupported_operation",
      "getUrl is not supported: the Arker VM API does not expose tunnel URLs",
      422,
    );
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
          "outboundCidrAllowlist",
        ]);
        const unsupported = Object.keys(values).filter((key) => !supported.has(key) && values[key] !== undefined);
        if (unsupported.length > 0) {
          throw new ArkerError(
            "unsupported_operation",
            `Arker create does not support option(s): ${unsupported.join(", ")}`,
            422,
          );
        }
        const sourceVmId = options?.snapshotId || config.sourceVmId || env("ARKER_SOURCE_VM");
        const sourceVmName = options?.templateId || config.sourceVmName || config.source || env("ARKER_SOURCE");
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
        const policies = networkPolicies(values);
        const request: ForkOptions = {
          ...sourceRequest,
          ...(options?.name ? { name: options.name } : {}),
          ...(resources ? { resources } : {}),
          ...(policies ? { policies } : {}),
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
