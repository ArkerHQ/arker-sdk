import { ArkerError, VM, withoutReadOnlyPolicyFields, type Arker, type ForkOptions, type Vm } from "../index.js";
import type { ArkerComputeProviderConfig } from "./arker-provider.js";

export function blaxelLifetimePolicies(ttl: unknown, configured: ArkerComputeProviderConfig["policies"] = {}) {
  if (ttl == null || ttl === "") return undefined;
  const match = typeof ttl === "string" ? /^([0-9]+)(s|m|h|d|w)$/.exec(ttl) : null;
  if (!match) throw new Error("Arker supports Blaxel ttl as whole-number s, m, h, d, or w; fractional and compound durations are unsupported");
  const seconds = Number(match[1]) * {s: 1, m: 60, h: 3600, d: 86400, w: 604800}[match[2] as "s" | "m" | "h" | "d" | "w"];
  if (seconds < 300) throw new Error("Blaxel ttl requires at least five minutes");
  if (seconds > 2592000) throw new Error("Arker lifetime schedules support Blaxel ttl up to 30 days");
  const rules = configured?.rules;
  if (rules != null && (typeof rules !== "object" || Array.isArray(rules))) throw new Error("rules must be a rule map");
  const id = "computesdk-blaxel-lifetime";
  if (configured && ("policies" in configured || Object.hasOwn(configured.rules ?? {}, id)))
    throw new Error("Blaxel ttl requires named policies with its lifetime policy id reserved");
  return {rules: {[id]: {type: "schedule" as const, after_created: seconds, action: "delete" as const}}};
}

export function validateBlaxelName(name: unknown, operation: "create" | "fork" = "create"): void {
  if (name == null || name === "") return;
  let message: string;
  if (typeof name !== "string") message = "Failed to parse sandbox body";
  else if (name.length > 49) message = "Invalid sandbox configuration: metadata.name is too long (maximum 49 characters)";
  else if (!/^[a-z0-9-]+$/.test(name)) message = "Invalid sandbox configuration: metadata.name must contain only lower case alphanumeric characters and hyphens";
  else return;
  if (operation === "fork") throw {code: 400, error: message};
  throw { code: "VALIDATION_FAILED", status_code: 400, message };
}

export function namedBlaxelFork(config: ArkerComputeProviderConfig = {}, getOrCreate = false, operation: "create" | "fork" = "create") {
  return async (client: Arker, request: ForkOptions): Promise<VM> => {
    validateBlaxelName(request.name, operation);
    if (!request.name) return client.fork(request);
    if ((config.baseUrl || process.env.ARKER_BASE_URL) && !(config.controlBaseUrl || process.env.ARKER_CONTROL_BASE_URL)) {
      throw new Error("Named Blaxel creation with a custom baseUrl requires controlBaseUrl for its public global gateway");
    }
    const headers: Record<string, string> = {
      "Arker-VM-Name": request.name,
      "Arker-VM-Name-Mode": getOrCreate ? "get_or_create" : "create",
    };
    for (const key of ["region", "provider"] as const) {
      const value = config[key] || client[key];
      if (value) headers[`x-arker-vm-${key}`] = value;
    }
    const prepared = request.policies
      ? { ...request, policies: withoutReadOnlyPolicyFields(request.policies) }
      : request;
    const deadline = Date.now() + 300_000;
    while (true) {
      try {
        const wire = await client._request<Vm>("POST", "/v1/named-fork", prepared, client.controlBaseUrl, headers,
          undefined, getOrCreate, { signal: AbortSignal.timeout(Math.max(0, deadline - Date.now())) });
        return new VM(client, wire.vm_id, client.controlBaseUrl, wire);
      } catch (error) {
        if (error instanceof ArkerError && error.status === 409 && error.code === "resource_busy") {
          const hint = error.body && "retry_after_seconds" in error.body ? error.body.retry_after_seconds : undefined;
          const delay = typeof hint === "number" && hint > 0 ? hint * 1000 : 1000;
          if (Date.now() + delay < deadline) {
            await new Promise((resolve) => setTimeout(resolve, delay));
            continue;
          }
        }
        // The pinned native SDK throws the API error object, not an Error subclass.
        if (operation === "create" && error instanceof ArkerError && error.status === 409 && error.code === "conflict"
            && error.body?.code === "conflict" && error.body.details.resource === "vm") {
          throw { code: "SANDBOX_ALREADY_EXISTS", message: error.message, status_code: 409 };
        }
        throw error;
      }
    }
  };
}


/** Apply creation envs to the image before its init or entrypoint executes. */
export function blaxelImageConfiguration(values: Record<string, unknown>, config: ArkerComputeProviderConfig = {}): ForkOptions["image_config"] | undefined {
  const envs = values.envs;
  if (envs === undefined) return undefined;
  if (!envs || typeof envs !== "object" || Array.isArray(envs)) throw new TypeError("Invalid Blaxel environment");
  const entries = Object.entries(envs);
  if (entries.length === 0) return undefined;
  if (typeof values.image !== "string" || !values.image) throw new TypeError("Blaxel creation envs require an image source");
  const environment: Record<string, string> = {};
  for (const [name, value] of entries) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name) || typeof value !== "string" || value.includes("\0")) {
      throw new TypeError("Invalid Blaxel environment entry");
    }
    Object.defineProperty(environment, name, { value, enumerable: true, writable: true, configurable: true });
  }
  return { ...config.imageConfig, env: { ...config.imageConfig?.env, ...environment } };
}

function stringLabels(value: unknown): value is Record<string, string> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.values(value).every(entry => typeof entry === "string");
}

export function blaxelCreationMetadata(labels: unknown, ttl?: unknown): Record<string, unknown> {
  labels ??= {};
  if (!stringLabels(labels)) throw new TypeError("Blaxel labels must be a string mapping");
  return { arker_blaxel: { version: 1, labels: { ...labels }, ...(ttl ? {ttl} : {}) } };
}

export function blaxelSandboxMetadata(vm: VM) {
  const stored = vm.metadata?.arker_blaxel;
  const labels = stored !== null && typeof stored === "object" && "version" in stored
    && stored.version === 1 && "labels" in stored && stringLabels(stored.labels) ? stored.labels : undefined;
  return { name: vm.name || vm.id, ...(vm.created_at ? { createdAt: vm.created_at } : {}),
    ...(labels && Object.keys(labels).length ? { labels: { ...labels } } : {}) };
}


export function blaxelSandboxSpec(vm: VM): import("@blaxel/core").SandboxInstance["spec"] {
  const stored = vm.metadata?.arker_blaxel;
  const ttl = stored && typeof stored === "object" && "version" in stored && stored.version === 1
    && "ttl" in stored ? stored.ttl : undefined;
  let expected: ReturnType<typeof blaxelLifetimePolicies>;
  try { expected = blaxelLifetimePolicies(ttl); } catch { return {runtime: undefined}; }
  const rule = vm.policies?.rules?.["computesdk-blaxel-lifetime"];
  const wanted = expected?.rules["computesdk-blaxel-lifetime"];
  if (rule && wanted && Object.entries(rule).filter(([,value]) => value != null).length === 3
      && rule.type === "schedule" && rule.action === wanted.action && rule.after_created === wanted.after_created) {
    return {runtime: {ttl: ttl as string}};
  }
  return {runtime: undefined};
}
