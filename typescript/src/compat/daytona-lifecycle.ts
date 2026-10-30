import { DaytonaInvalidArgumentError, DaytonaTimeoutError } from "@daytona/sdk";
import { ArkerError, VM, type ForkRequest, type Vm } from "../index.js";
import type { ArkerComputeProviderConfig } from "./arker-provider.js";
import { assertSupportedObjectKeys } from "./common.js";
import { translateArker } from "./daytona-volumes.js";

function budget(timeout: number) {
  if (!Number.isFinite(timeout) || timeout < 0) throw new DaytonaInvalidArgumentError("Timeout must be a non-negative number");
  return timeout === 0 ? {} : { signal: AbortSignal.timeout(Math.ceil(timeout * 1000)) };
}

type Transport = ReturnType<typeof budget>;

function translate(error: unknown, transport: Transport): never {
  if (transport.signal?.aborted && error === transport.signal.reason) {
    throw new DaytonaTimeoutError("Lifecycle request timed out; its outcome may be pending");
  }
  return translateArker(error);
}

async function transition(vm: VM, operation: "pause" | "resume", transport: Transport): Promise<void> {
  try {
    const result = await vm._client._request<{ vm_id: string; state: string; mode?: string }>(
      "POST", `/v1/vms/${encodeURIComponent(vm.id)}/${operation}`,
      operation === "pause" ? { mode: "full" } : {}, vm.baseUrl,
      undefined, undefined, false, transport,
    );
    if (result.vm_id !== vm.id || result.state !== (operation === "pause" ? "paused" : "running") || (operation === "pause" && result.mode !== "full")) {
      throw new Error("Invalid lifecycle response");
    }
  } catch (error) { translate(error, transport); }
}

export function pause(vm: VM, timeout = 60): Promise<void> {
  return transition(vm, "pause", budget(timeout));
}

export function start(vm: VM, timeout = 60): Promise<void> {
  return transition(vm, "resume", budget(timeout));
}

export function unsupported(operation: "stop" | "archive"): never {
  throw new ArkerError("unsupported_operation", operation === "stop"
    ? "Daytona stop requires guest SIGTERM/SIGKILL shutdown and a durable stopped state; Arker pause cannot substitute for shutdown"
    : "Daytona archive requires atomic stopped-state validation and filesystem offload; Arker pause does not provide that archive contract", 422);
}

export async function fork(vm: VM, params?: { name?: string }, timeout = 60, config: ArkerComputeProviderConfig = {}): Promise<VM> {
  assertSupportedObjectKeys(params, ["name"], "daytona", "Sandbox.fork");
  const transport = budget(timeout);
  const request: Partial<ForkRequest> = { source_vm_id: vm.id, layers: ["disk"], durable: false };
  let child: VM;
  try {
    if (params?.name === undefined) {
      child = await vm.fork(request, transport);
    } else {
      if (typeof params.name !== "string") throw new TypeError("name must be a string");
      if ((config.baseUrl || process.env.ARKER_BASE_URL) && !(config.controlBaseUrl || process.env.ARKER_CONTROL_BASE_URL)) {
        throw new Error("Named forks with a custom baseUrl require an explicit controlBaseUrl");
      }
      const client = vm._client;
      const headers: Record<string, string> = { "Arker-VM-Name": params.name, "Arker-VM-Name-Mode": "create" };
      if (client.region) headers["x-arker-vm-region"] = client.region;
      if (client.provider) headers["x-arker-vm-provider"] = client.provider;
      const wire = await client._request<Vm>("POST", "/v1/named-fork", { ...request, name: params.name }, client.controlBaseUrl, headers, undefined, false, transport);
      child = new VM(client, wire.vm_id, client.controlBaseUrl, wire);
    }
  } catch (error) { translate(error, transport); }
  // Preserve the error class and accepted child ID; failure is not proof of deletion.
  try { await transition(child, "resume", transport); }
  catch (error) {
    if (error instanceof Error) Object.assign(error, { sandboxId: child.id });
    throw error;
  }
  return child;
}
