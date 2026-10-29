import { e2bAdmissionError } from "./e2b-errors.js";
import { sandboxInfo, E2B_LIFETIME_POLICY_ID } from "./e2b-inventory.js";
import {
  InvalidArgumentError,
  SandboxNotFoundError,
  SandboxNotRunningError,
  type Sandbox as NativeSandbox,
  type SandboxConnectOpts,
  type SandboxLifecycle,
  type SandboxPauseOpts,
} from "e2b";
import { ArkerError, type VM } from "../index.js";
import { assertSupportedObjectKeys, type UniversalSandbox } from "./common.js";

export { E2B_LIFETIME_POLICY_ID } from "./e2b-inventory.js";

type NativeLifecycle = Pick<NativeSandbox, "pause" | "connect" | "setTimeout" | "isRunning" | "getInfo">;
type RequestOptions = Parameters<NativeSandbox["getInfo"]>[0];

function unsupported(message: string): never {
  throw new ArkerError("unsupported_operation", message, 422);
}

/** Validate before creating an Arker sandbox; native options pass through unchanged. */
export function validateArkerE2BLifecycle(lifecycle?: SandboxLifecycle): void {
  if (lifecycle === undefined) return;
  assertSupportedObjectKeys(lifecycle, ["onTimeout", "autoResume"], "e2b", "Sandbox.create.lifecycle");
  const timeout = lifecycle.onTimeout;
  const action = typeof timeout === "string" ? timeout : timeout?.action;
  if (action !== "kill" && action !== "pause") {
    throw new InvalidArgumentError("onTimeout must be 'kill' or 'pause'");
  }
  if (typeof timeout === "object") {
    assertSupportedObjectKeys(timeout, action === "pause" ? ["action", "mode", "keepMemory"] : ["action"], "e2b", "Sandbox.create.lifecycle.onTimeout");
  }
  if (lifecycle.autoResume !== undefined && typeof lifecycle.autoResume !== "boolean") {
    throw new InvalidArgumentError("autoResume must be a boolean");
  }
  if (action === "kill" && lifecycle.autoResume === true) {
    throw new InvalidArgumentError("autoResume requires onTimeout: 'pause'");
  }
  if (action === "pause") {
    unsupported("Arker cannot preserve E2B auto-pause and autoResume semantics with a scheduled suspension");
  }
}

function timeoutDuration(timeoutMs: number): number {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 86_400_000) {
    throw new InvalidArgumentError("timeoutMs must be positive, finite, and no greater than 86400000");
  }
  // E2B sends whole seconds, rounding up, to its API.
  return Math.ceil(timeoutMs / 1000) * 1000;
}

/** Lifecycle operations shared by the E2B facade's instance and static methods.
 * Paused-operation semantics require backend admission to reject the durable
 * marker with explicit_resume. A client flag or GET-before-run cannot enforce
 * this across clients. Signed-file transport coverage and full lifecycle parity
 * still require backend and live differential validation.
 */
export class E2BLifecycle {
  constructor(private readonly box: UniversalSandbox, private readonly native?: NativeLifecycle) {}

  private vm(): VM {
    if (this.box.provider !== "arker") unsupported("This provider does not expose an E2B lifecycle handle");
    return (this.box as UniversalSandbox & { getInstance(): VM }).getInstance();
  }

  private async current(): Promise<VM> {
    try {
      return await this.vm().refresh();
    } catch (error) {
      throw lifecycleError(error, this.box.sandboxId);
    }
  }

  async pause(opts?: SandboxPauseOpts): Promise<boolean> {
    if (this.native) return this.native.pause(opts);
    assertSupportedObjectKeys(opts, ["mode", "keepMemory"], "e2b", "Sandbox.pause");
    if (opts?.mode !== undefined && opts.keepMemory !== undefined) throw new InvalidArgumentError("Pass either mode or keepMemory, not both");
    if (opts?.mode !== undefined && opts.mode !== "full" && opts.mode !== "filesystem") throw new InvalidArgumentError("mode must be 'full' or 'filesystem'");
    if (opts?.keepMemory !== undefined && typeof opts.keepMemory !== "boolean") throw new InvalidArgumentError("keepMemory must be a boolean");
    const mode = opts?.mode ?? (opts?.keepMemory === false ? "filesystem" : "full");
    const response = await this.request("POST", "pause", { mode, deadline: { policy_id: E2B_LIFETIME_POLICY_ID } });
    this.checkResponse(response, "paused");
    if (typeof response.already_paused !== "boolean" || (!response.already_paused && response.mode !== mode)) {
      throw new Error("Invalid Arker pause response");
    }
    return !response.already_paused;
  }

  async betaPause(opts?: SandboxPauseOpts): Promise<boolean> {
    return this.pause(opts);
  }

  /** The facade must await this method and return its own `this`. */
  async connect(opts?: SandboxConnectOpts): Promise<this> {
    if (this.native) {
      await this.native.connect(opts);
      return this;
    }
    assertSupportedObjectKeys(opts, ["timeoutMs", "onResume"], "e2b", "Sandbox.connect");
    if (opts?.onResume !== undefined && opts.onResume !== "restore" && opts.onResume !== "reboot") {
      throw new InvalidArgumentError("onResume must be 'restore' or 'reboot'");
    }
    const duration = timeoutDuration(opts?.timeoutMs ?? 300_000);
    const body = {
      deadline: { policy_id: E2B_LIFETIME_POLICY_ID, timeout_seconds: duration / 1000 },
      ...(opts?.onResume === "reboot" ? { mode: "filesystem" } : {}),
    };
    const response = await this.request("POST", "resume", body);
    this.checkResponse(response, "running");
    if (opts?.onResume === "reboot" && response.mode !== "filesystem") throw new Error("Invalid Arker resume mode");
    return this;
  }

  async setTimeout(timeoutMs: number, opts?: RequestOptions): Promise<void> {
    if (this.native) return this.native.setTimeout(timeoutMs, opts);
    assertSupportedObjectKeys(opts, [], "e2b", "Sandbox.setTimeout");
    const duration = timeoutDuration(timeoutMs);
    let response: Record<string, unknown>;
    try {
      response = await this.request("POST", "deadline", { policy_id: E2B_LIFETIME_POLICY_ID, timeout_seconds: duration / 1000 });
    } catch (error) {
      if (error instanceof ArkerError && e2bAdmissionError(error) instanceof SandboxNotRunningError) throw new SandboxNotFoundError(`Sandbox ${this.box.sandboxId} not found`);
      throw error;
    }
    if (response.vm_id !== this.vm().id || typeof response.deadline_at !== "string" || !Number.isFinite(Date.parse(response.deadline_at))) {
      throw new Error("Invalid Arker deadline response");
    }
  }

  async isRunning(opts?: RequestOptions): Promise<boolean> {
    if (this.native) return this.native.isRunning(opts);
    assertSupportedObjectKeys(opts, [], "e2b", "Sandbox.isRunning");
    let response: Record<string, unknown>;
    try { response = await this.request("GET", "lifecycle"); }
    catch (error) {
      if (error instanceof SandboxNotFoundError) return false;
      throw error;
    }
    if (response.vm_id !== this.vm().id || ![null, "full", "filesystem"].includes(response.mode as string | null)) {
      throw new Error("Invalid Arker lifecycle response");
    }
    if (response.state === "paused" && response.customer_paused === true) {
      this.checkResponse(response, "paused");
      return false;
    }
    if ((response.state === "running" || response.state === "idle") && response.customer_paused === false) return true;
    throw new Error("Inconsistent Arker lifecycle state");
  }

  private async request(method: "GET" | "POST", operation: string, body?: unknown): Promise<Record<string, unknown>> {
    const vm = this.vm();
    try {
      return await vm._client._request(method, `/v1/vms/${encodeURIComponent(vm.id)}/${operation}`, body, vm.baseUrl);
    } catch (error) { throw lifecycleError(error, this.box.sandboxId); }
  }

  private checkResponse(response: Record<string, unknown>, state: "running" | "paused"): void {
    if (response.vm_id !== this.vm().id || response.state !== state ||
        (response.mode !== "full" && response.mode !== "filesystem")) {
      throw new Error("Invalid Arker lifecycle response");
    }
  }

  async getInfo(opts?: RequestOptions): Promise<Awaited<ReturnType<NativeSandbox["getInfo"]>>> {
    if (this.native) return this.native.getInfo(opts);
    assertSupportedObjectKeys(opts, [], "e2b", "Sandbox.getInfo");
    const vm = await this.current();
    const lifecycle = await this.request("GET", "lifecycle");
    return sandboxInfo(vm, lifecycle);
  }
}

function lifecycleError(error: unknown, sandboxId: string): unknown {
  return error instanceof ArkerError && error.status === 404
    ? new SandboxNotFoundError(`Sandbox ${sandboxId} not found`)
    : error;
}
