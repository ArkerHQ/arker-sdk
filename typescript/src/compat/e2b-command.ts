import { setTimeout as delay } from "node:timers/promises";
import {
  CommandExitError,
  SandboxError,
  TimeoutError,
  NotFoundError,
  type CommandHandle,
  type CommandStartOpts,
} from "e2b";
import {
  GuestProcessError,
  type GuestProcesses,
  type GuestProcessResult,
} from "./guest-process.js";
import signals from "./linux-signals.generated.js";
import { assertSupportedObjectKeys } from "./common.js";

type Control = Pick<
  GuestProcesses,
  "get" | "kill" | "writeStdin" | "closeStdin"
>;
type Options = Pick<CommandStartOpts, "timeoutMs" | "onStdout" | "onStderr">;

export function e2bProcessError(error: unknown): unknown {
  return error instanceof GuestProcessError &&
    error.code === "process_not_found"
    ? new NotFoundError(error.message)
    : error;
}

export async function killE2BProcess(manager: Pick<GuestProcesses, "get" | "kill">, pid: string): Promise<boolean> {
  try {
    if ((await manager.get(pid)).status !== "running") return false;
  } catch (error) {
    if (error instanceof GuestProcessError && error.code === "process_not_found") return false;
    throw error;
  }
  await manager.kill(pid);
  return true;
}

// keyof excludes vendor-private fields but makes additions to the public handle fail typechecking.
class CompatCommandHandle implements Pick<CommandHandle, keyof CommandHandle> {
  readonly pid: number;
  #controller = new AbortController();
  #stdout = "";
  #stderr = "";
  #result?: {
    stdout: string;
    stderr: string;
    exitCode: number;
    error?: string;
  };
  #error?: unknown;
  #task: Promise<void>;
  #disconnected = false;

  constructor(
    private manager: Control,
    private started: GuestProcessResult,
    private options?: Options,
  ) {
    this.pid = Number(started.pid);
    const timeout = options?.timeoutMs ?? 60_000;
    if (!Number.isFinite(timeout) || timeout < 0)
      throw new RangeError("timeoutMs must be nonnegative and finite");
    this.#task = this.consume(
      timeout === 0 ? undefined : performance.now() + timeout,
    );
  }

  get stdout() {
    return this.#stdout;
  }
  get stderr() {
    return this.#stderr;
  }
  get exitCode() {
    return this.#result?.exitCode;
  }
  get error() {
    return this.#result?.error;
  }

  private async consume(deadline?: number): Promise<void> {
    try {
      while (!this.#disconnected) {
        if (deadline !== undefined && performance.now() >= deadline)
          throw new TimeoutError("Timed out waiting for command");
        const value = await this.manager.get(this.started.pid, {
          signal: this.#controller.signal,
          rawExitCode: true,
        });
        if (this.#disconnected) return;
        if (deadline !== undefined && performance.now() >= deadline)
          throw new TimeoutError("Timed out waiting for command");
        const stdout = value.stdout.slice(this.#stdout.length);
        const stderr = value.stderr.slice(this.#stderr.length);
        this.#stdout = value.stdout;
        this.#stderr = value.stderr;
        if (stdout) await this.options?.onStdout?.(stdout);
        if (this.#disconnected) return;
        if (stderr) await this.options?.onStderr?.(stderr);
        if (this.#disconnected) return;
        if (value.status !== "running") {
          const code = value.exitCode;
          this.#result = {
            stdout: this.#stdout,
            stderr: this.#stderr,
            exitCode: code < 0 ? -1 : code,
            error:
              code < 0
                ? "signal: " + (signals[String(-code)] ?? "signal " + -code)
                : code === 0
                  ? undefined
                  : "exit status " + code,
          };
          return;
        }
        const interval =
          deadline === undefined
            ? 50
            : Math.max(0, Math.min(50, deadline - performance.now()));
        await delay(interval, undefined, { signal: this.#controller.signal });
      }
    } catch (error) {
      if (!this.#disconnected) this.#error = e2bProcessError(error);
    } finally {
      this.#controller.abort();
    }
  }

  async wait() {
    await this.#task;
    if (this.#error !== undefined) throw this.#error;
    if (!this.#result)
      throw new SandboxError("Process exited without a result");
    if (this.#result.exitCode !== 0) throw new CommandExitError(this.#result);
    return this.#result;
  }

  async disconnect(): Promise<void> {
    this.#disconnected = true;
    this.#controller.abort();
  }

  async kill(): Promise<boolean> {
    return killE2BProcess(this.manager, this.started.pid);
  }

  async sendStdin(
    data: string | Uint8Array,
    options?: Parameters<CommandHandle["sendStdin"]>[1],
  ): Promise<void> {
    assertSupportedObjectKeys(options, [], "e2b", "CommandHandle.sendStdin");
    await this.manager.writeStdin(this.started.pid, data).catch((error) => {
      throw e2bProcessError(error);
    });
  }

  async closeStdin(
    options?: Parameters<CommandHandle["closeStdin"]>[0],
  ): Promise<void> {
    assertSupportedObjectKeys(options, [], "e2b", "CommandHandle.closeStdin");
    await this.manager.closeStdin(this.started.pid).catch((error) => {
      throw e2bProcessError(error);
    });
  }
}

export function e2bCommandHandle(
  manager: Control,
  started: GuestProcessResult,
  options?: Options,
): CommandHandle {
  // The vendor exports only a nominal type, not its constructor; all public members are checked above.
  return new CompatCommandHandle(
    manager,
    started,
    options,
  ) as unknown as CommandHandle;
}
