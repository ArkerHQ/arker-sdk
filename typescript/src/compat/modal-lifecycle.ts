import { NotFoundError } from "modal";
import { type Arker, type VM, type RequestTransportOptions } from "../index.js";
import { isArkerVmIdentifier } from "./router.js";
import { missingVm } from "./modal-errors.js";

type Reason = "deadline" | "requested" | "unknown" | null;
export async function lifecycle(vm: VM, transport?: RequestTransportOptions): Promise<Reason> {
  let result;
  try {
    result = await vm._client._request("GET", `/v1/vms/${encodeURIComponent(vm.id)}/lifecycle?include_terminated=true`, undefined, vm.baseUrl, undefined, undefined, undefined, transport);
  } catch (error) {
    if (!missingVm(error)) throw error;
    throw Object.assign(new NotFoundError(`Sandbox ${vm.id} not found`), { cause: error });
  }
  if (!result || typeof result !== "object") throw new Error("Invalid sandbox lifecycle response");
  const value = result as Record<string, unknown>;
  if (value.vm_id !== vm.id || !["running", "idle", "paused", "terminated"].includes(String(value.state))) throw new Error("Invalid sandbox lifecycle response");
  if (value.state !== "terminated") return null;
  if (!["deadline", "requested", "unknown"].includes(String(value.termination_reason)) || typeof value.terminated_at !== "string") throw new Error("Invalid retained sandbox lifecycle response");
  return value.termination_reason as Reason;
}
export function returncode(reason: Reason): number | null {
  if (reason === null) return null;
  if (reason === "deadline") return 124;
  if (reason === "requested") return 137;
  throw new Error("The retained sandbox has no recorded terminal result");
}
export async function lookup(client: Arker, id: string, transport?: RequestTransportOptions): Promise<VM> {
  try { return await client.getVm(id, undefined, transport); }
  catch (error) { if (!missingVm(error) || !isArkerVmIdentifier(id)) throw error; }
  const vm = client.vm(id);
  await lifecycle(vm, transport);
  return vm;
}
