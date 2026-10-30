import { createHash, randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import type { SandboxInterface } from "computesdk";
import type { VM } from "../index.js";
import source from "./guest-process.generated.js";
import { shellQuote } from "./common.js";

// Nested base64 control frames must fit in a guest shell argument.
const STDIN_CHUNK_BYTES = 32 * 1024;

export interface GuestProcessResult {
  name: string;
  command: string;
  workingDir: string;
  pid: string;
  status: "running" | "completed" | "failed" | "stopped" | "killed";
  exitCode: number;
  startedAt: string;
  completedAt: string;
  stdout: string;
  stderr: string;
  logs: string;
  stdin: boolean;
  tty?: { rows: number; cols: number };
}

type Callbacks = {
  onStdout?: (chunk: string) => void;
  onStderr?: (chunk: string) => void;
  onLog?: (chunk: string) => void;
};

export class GuestProcessError extends Error {
  constructor(message: string, readonly code?: string) { super(message); this.name = "GuestProcessError"; }
}

export class GuestProcesses {
  private installed?: Promise<unknown>;
  private controlSession?: Promise<string>;
  private pending: Promise<unknown> = Promise.resolve();
  private readonly path = `/usr/local/lib/arker-compat/guest-process-${createHash("sha256").update(source).digest("hex").slice(0, 16)}.py`;

  constructor(private readonly sandbox: SandboxInterface) {}

  private call<T>(action: string, values: Record<string, unknown> = {}): Promise<T> {
    return this.schedule(() => this.callOnce<T>(action, values));
  }

  private schedule<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.pending.then(operation);
    this.pending = result.then(() => undefined, () => undefined);
    return result;
  }

  private async callOnce<T>(action: string, values: Record<string, unknown>): Promise<T> {
    this.installed ??= this.sandbox.filesystem.writeFile(this.path, source).catch((error) => {
      this.installed = undefined;
      throw error;
    });
    await this.installed;
    const payload = Buffer.from(JSON.stringify({ action, ...values })).toString("base64");
    let response: { stdout: string; stderr: string; exitCode: number };
    if (action === "exec") {
      const vm = (this.sandbox as unknown as { getInstance(): VM }).getInstance();
      const session = await vm.createSession();
      const started = await vm.run(`python3 -I ${shellQuote(this.path)} supervise ${shellQuote(payload)}`, {
        session_id: session.session_id, time_to_background: 0,
      });
      const deadline = Date.now() + 300_000;
      while (true) {
        const current = await vm.getRun(started.runId);
        if (current.stdout.includes("\n")) {
          response = { stdout: current.stdout.split("\n", 1)[0]!, stderr: current.stderr, exitCode: 0 };
          break;
        }
        if (!["pending", "running"].includes(current.state)) {
          throw new Error(current.stderr || "Guest process supervisor stopped before reporting readiness");
        }
        if (Date.now() >= deadline) throw new Error("Guest process supervisor did not report readiness");
        await delay(20);
      }
    } else {
      const vm = (this.sandbox as unknown as { getInstance(): VM }).getInstance();
      this.controlSession ??= vm.createSession().then((session) => session.session_id).catch((error) => {
        this.controlSession = undefined;
        throw error;
      });
      const completed = await vm.run(`python3 -I ${shellQuote(this.path)} call ${shellQuote(payload)}`, {
        session_id: await this.controlSession,
      });
      if (completed.type !== "completed") throw new Error("Guest process control did not complete synchronously");
      response = completed;
    }
    let parsed: { error?: string; code?: string; result: T };
    try { parsed = JSON.parse(response.stdout); }
    catch { throw new Error(response.stderr || "Guest process agent returned invalid JSON"); }
    if (parsed.error) throw new GuestProcessError(parsed.error, parsed.code);
    if (response.exitCode !== 0) throw new Error(response.stderr || "Guest process agent failed");
    return parsed.result;
  }

  async exec(request: Record<string, any> & Callbacks): Promise<GuestProcessResult> {
    const { onStdout, onStderr, onLog, waitForCompletion = false, ...values } = request;
    values.name ??= randomUUID();
    values._requestId = randomUUID();
    const current = await this.call<GuestProcessResult>("exec", values);
    return waitForCompletion
      ? this.waitFor(current.name, { maxWait: -1, interval: 50 }, { onStdout, onStderr, onLog })
      : current;
  }

  get(identifier: string, options?: { signal?: AbortSignal; retry?: boolean; rawExitCode?: boolean }): Promise<GuestProcessResult> {
    options?.signal?.throwIfAborted();
    return this.call<GuestProcessResult>("get", { identifier, ...(options?.rawExitCode ? { rawExitCode: true } : {}) }).then((result) => {
      options?.signal?.throwIfAborted();
      return result;
    });
  }

  list(options?: { includeOutput?: boolean }): Promise<GuestProcessResult[]> { return this.call("list", options); }

  async readOutput(identifier: string, stream: "stdout" | "stderr", offset: number, signal?: AbortSignal): Promise<{ data: Uint8Array; nextOffset: number; size: number; eof: boolean; exitCode: number | null }> {
    signal?.throwIfAborted();
    const result = await this.call<{ data: string; nextOffset: number; size: number; eof: boolean; exitCode: number | null }>("readStream", { identifier, stream, offset, limit: 65536 });
    signal?.throwIfAborted();
    return { ...result, data: Buffer.from(result.data, "base64") };
  }

  private async waitFor(identifier: string, options: { maxWait?: number; interval?: number; signal?: AbortSignal }, callbacks: Callbacks = {}): Promise<GuestProcessResult> {
    const maxWait = options.maxWait ?? 60_000;
    const interval = options.interval ?? 1000;
    if (maxWait < -1 || interval <= 0) throw new Error("maxWait must be -1 or nonnegative; interval must be positive");
    const deadline = maxWait === -1 ? undefined : Date.now() + maxWait;
    let stdoutOffset = 0;
    let stderrOffset = 0;
    while (true) {
      options.signal?.throwIfAborted();
      if (deadline !== undefined && Date.now() >= deadline) throw new Error(`Timed out waiting for process ${identifier}`);
      const current = await this.get(identifier, options);
      if (deadline !== undefined && Date.now() > deadline) throw new Error(`Timed out waiting for process ${identifier}`);
      const stdout = current.stdout.slice(stdoutOffset);
      const stderr = current.stderr.slice(stderrOffset);
      if (stdout) { callbacks.onStdout?.(stdout); callbacks.onLog?.(stdout); }
      if (stderr) { callbacks.onStderr?.(stderr); callbacks.onLog?.(stderr); }
      stdoutOffset = current.stdout.length;
      stderrOffset = current.stderr.length;
      if (current.status !== "running") return current;
      await delay(deadline === undefined ? interval : Math.max(0, Math.min(interval, deadline - Date.now())), undefined, { signal: options.signal });
    }
  }

  wait(identifier: string, options: { maxWait?: number; interval?: number; signal?: AbortSignal } = {}): Promise<GuestProcessResult> {
    return this.waitFor(identifier, options);
  }

  kill(identifier: string): Promise<{ message: string }> { return this.call("kill", { identifier }); }
  stop(identifier: string): Promise<{ message: string }> { return this.call("stop", { identifier }); }

  async logs(identifier: string, type: "stdout" | "stderr" | "all" = "all"): Promise<string> {
    if (!["stdout", "stderr", "all"].includes(type)) throw new Error("Unsupported log type");
    const result = await this.get(identifier);
    return type === "all" ? result.logs : result[type];
  }

  writeStdin(identifier: string, data: string | Uint8Array): Promise<{ message: string }> {
    const bytes = Buffer.from(data);
    return this.schedule(async () => {
      let result: { message: string } | undefined;
      for (let offset = 0; offset < Math.max(1, bytes.length); offset += STDIN_CHUNK_BYTES) {
        result = await this.callOnce("writeStdin", { identifier, data: bytes.subarray(offset, offset + STDIN_CHUNK_BYTES).toString("base64") });
      }
      return result!;
    });
  }
  resize(identifier: string, rows: number, cols: number): Promise<{ message: string }> {
    return this.call("resize", { identifier, tty: { rows, cols } });
  }
  closeStdin(identifier: string): Promise<{ message: string }> { return this.call("closeStdin", { identifier }); }
}
