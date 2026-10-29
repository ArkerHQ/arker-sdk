import { randomUUID } from "node:crypto";
import type { VM } from "../index.js";

export interface E2BNetworkOptions { allowPublicTraffic?: boolean }

export function validateE2BNetwork(network: E2BNetworkOptions | undefined): void {
  if (network === undefined) return;
  if (!network || typeof network !== "object" || Array.isArray(network)) throw new TypeError("network must be an object");
  const unsupported = Object.keys(network).filter(key => key !== "allowPublicTraffic");
  if (unsupported.length) throw new TypeError(`Unsupported network option(s): ${unsupported.join(", ")}`);
  if (network.allowPublicTraffic !== undefined && typeof network.allowPublicTraffic !== "boolean") {
    throw new TypeError("network.allowPublicTraffic must be a boolean");
  }
}

/** Keep custom ingress intact; default exposure applies only without custom inbound rules. */
export async function configureE2BIngress(vm: VM, allowPublicTraffic: boolean): Promise<void> {
  const current = await vm.refresh();
  const inbound = Object.values(current.policies?.rules ?? {}).filter(rule => rule.type === "inbound");
  if (inbound.length) {
    if (!allowPublicTraffic && inbound.some(rule => rule.type === "inbound" && rule.action !== "deny" && rule.auth !== undefined && rule.auth !== "arker")) {
      throw new Error("network.allowPublicTraffic:false conflicts with an existing public inbound policy");
    }
    return;
  }
  await vm.update({ policies: { rules: {
    [`e2b-app-ingress-${randomUUID()}`]: { type: "inbound", priority: 0, match: { ports: [[1, 49982], [49984, 65535]] }, action: "allow", auth: allowPublicTraffic ? "open" : "arker" },
    [`e2b-envd-ingress-${randomUUID()}`]: { type: "inbound", priority: 1, match: { ports: [49983] }, action: "allow", auth: "arker" },
  } } });
}
