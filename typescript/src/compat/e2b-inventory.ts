import type { SandboxInfo } from "e2b";
import { ArkerError, type VM } from "../index.js";

export const E2B_LIFETIME_POLICY_ID = "computesdk-e2b-lifetime";

function unavailable(message: string): never {
  throw new ArkerError("unsupported_operation", message, 422);
}
function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}
function timestamp(value: unknown, name: string): Date {
  if (typeof value !== "string" || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) unavailable(`Arker does not expose ${name}`);
  const result = new Date(value);
  if (!Number.isFinite(result.getTime())) unavailable(`Arker returned an invalid ${name}`);
  return result;
}

/** Map only recorded provenance, concrete resources, and authoritative lifecycle timestamps. */
export function sandboxInfo(vm: VM, lifecycle: Record<string, unknown>): SandboxInfo {
  const descriptor = record(vm.metadata?.arker_e2b);
  if (descriptor?.version !== 1) unavailable("VM has no supported E2B inventory descriptor");
  const metadata = record(descriptor.metadata);
  if (!metadata || Object.values(metadata).some(value => typeof value !== "string")) unavailable("E2B inventory metadata is not a string map");
  const source = record(descriptor.source);
  if (!source || !["vm_id", "vm_name"].includes(String(source.kind)) || typeof source.value !== "string" || !source.value) {
    unavailable("E2B inventory has no recorded source reference");
  }
  if (typeof descriptor.envd_version !== "string" || !descriptor.envd_version) unavailable("E2B inventory has no recorded envd version");
  const cpu = vm.resources?.vcpu, memory = vm.resources?.memory_mib;
  if (!Number.isSafeInteger(cpu) || !Number.isSafeInteger(memory) || cpu! <= 0 || memory! <= 0) {
    unavailable("Arker does not expose concrete CPU and memory sizes for this VM");
  }
  if (lifecycle.vm_id !== vm.id || !["running", "idle", "paused"].includes(String(lifecycle.state)) ||
      typeof lifecycle.customer_paused !== "boolean" || (lifecycle.state === "paused") !== lifecycle.customer_paused) {
    throw new Error("Inconsistent Arker lifecycle state");
  }
  let end: Date;
  if (lifecycle.customer_paused) {
    end = timestamp(lifecycle.paused_at, "sandbox pause timestamp");
  } else {
    const rule = vm.policies?.rules?.[E2B_LIFETIME_POLICY_ID];
    if (rule?.type !== "schedule" || rule.action !== "delete") unavailable("VM has no E2B lifetime delete schedule");
    if (rule.at !== undefined) end = timestamp(rule.at, "sandbox expiration timestamp");
    else if (typeof rule.after_created === "number" && Number.isFinite(rule.after_created)) {
      end = new Date(timestamp(vm.created_at, "VM creation timestamp").getTime() + rule.after_created * 1000);
    } else unavailable("E2B lifetime schedule uses an unsupported clock");
  }
  if (!Number.isFinite(end.getTime())) unavailable("Arker returned an invalid sandbox expiration timestamp");
  const inbound = Object.values(vm.policies?.rules ?? {}).filter(rule => rule.type === "inbound");
  const apps = inbound.find(rule => rule.type === "inbound" && rule.action === "allow" && rule.expires_at === undefined &&
    JSON.stringify(rule.match) === JSON.stringify({ ports: [[1, 49982], [49984, 65535]] }));
  const control = inbound.find(rule => rule.type === "inbound" && rule.action === "allow" && rule.expires_at === undefined && rule.auth === "arker" &&
    JSON.stringify(rule.match) === JSON.stringify({ ports: [49983] }));
  const publicTraffic = inbound.length === 2 && control && apps?.type === "inbound" && ["arker", "open"].includes(String(apps.auth))
    ? apps.auth === "open" : undefined;
  return {
    sandboxId: vm.id, templateId: source.value,
    ...(source.kind === "vm_name" ? { name: source.value } : {}),
    metadata: { ...metadata } as Record<string, string>,
    startedAt: timestamp(lifecycle.started_at, "sandbox start timestamp"),
    endAt: end,
    state: lifecycle.customer_paused ? "paused" : "running",
    cpuCount: cpu!, memoryMB: memory!, envdVersion: descriptor.envd_version,
    ...(publicTraffic === undefined ? {} : { network: { allowPublicTraffic: publicTraffic } }),
  };
}
