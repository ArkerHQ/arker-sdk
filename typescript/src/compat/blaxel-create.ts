import { ArkerError, VM, withoutReadOnlyPolicyFields, type Arker, type ForkOptions, type Vm } from "../index.js";
import type { ArkerComputeProviderConfig } from "./arker-provider.js";

export function namedBlaxelFork(config: ArkerComputeProviderConfig = {}, getOrCreate = false) {
  return async (client: Arker, request: ForkOptions): Promise<VM> => {
    if (!request.name) return client.fork(request);
    if (!/^[a-z0-9-]+$/.test(request.name)) {
      throw { code: "VALIDATION_FAILED", status_code: 400, message: "metadata.name must contain only lower case alphanumeric characters and hyphens" };
    }
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
        const wire = await client._request<Vm>("POST", "/v1/fork", prepared, client.controlBaseUrl, headers,
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
        if (error instanceof ArkerError && error.status === 409 && error.code === "conflict"
            && error.body?.code === "conflict" && error.body.details.resource === "vm") {
          throw { code: "SANDBOX_ALREADY_EXISTS", message: error.message, status_code: 409 };
        }
        throw error;
      }
    }
  };
}
