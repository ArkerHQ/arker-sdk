import { randomBytes } from "node:crypto";
import type { Arker, ForkOptions, VM } from "../index.js";

export interface E2BNetworkOptions { allowPublicTraffic?: boolean }
const APP = "computesdk-e2b-apps";
const CONTROL = "computesdk-e2b-envd";
const SECRET = "computesdk-e2b-traffic";
const PORTS = [[1, 49982], [49984, 65535]];
const tokens = new WeakMap<VM, string | null>();
export const trafficToken = (vm: VM): string | undefined => tokens.get(vm) ?? undefined;
export const createdTrafficToken = (vm: VM): string | null | undefined => tokens.get(vm);
export function rememberTrafficToken(vm: VM, token: string | null): void { tokens.set(vm, token); }

export function validateE2BNetwork(network: E2BNetworkOptions | undefined): void {
  if (network === undefined) return;
  if (!network || typeof network !== "object" || Array.isArray(network)) throw new TypeError("network must be an object");
  const unsupported = Object.keys(network).filter(key => key !== "allowPublicTraffic");
  if (unsupported.length) throw new TypeError(`Unsupported network option(s): ${unsupported.join(", ")}`);
  if (network.allowPublicTraffic !== undefined && typeof network.allowPublicTraffic !== "boolean") throw new TypeError("network.allowPublicTraffic must be a boolean");
}

type Policies = NonNullable<ForkOptions["policies"]>;
type Rule = NonNullable<Policies["rules"]>[string];
function managed(id: string, rule: Rule): boolean {
  if (!rule || rule.type !== "inbound" || rule.action !== "allow") return false;
  if (Object.keys(rule).some(key => !["type", "priority", "action", "auth", "match"].includes(key))
      || Object.keys(rule.match ?? {}).some(key => !["ports", "headers"].includes(key))) return false;
  const headers = rule.match?.headers;
  if (headers && (Object.keys(headers).length !== 1 || headers["e2b-traffic-access-token"]?.length !== 1 || !headers["e2b-traffic-access-token"]?.every(value => value === "***" || value === `\${secret:${SECRET}}`))) return false;
  const ports = JSON.stringify(rule.match?.ports);
  return ((id === APP || /^e2b-app-ingress-[0-9a-f-]{36}$/.test(id)) && ports === JSON.stringify(PORTS) && ["arker", "open"].includes(String(rule.auth)) && (!headers || rule.auth === "open"))
    || ((id === CONTROL || /^e2b-envd-ingress-[0-9a-f-]{36}$/.test(id)) && ports === "[49983]" && rule.auth === "arker" && !headers);
}

/** Replace only managed ingress in the fork transaction; custom rules stay authoritative. */
export function trafficPolicies(source: Policies | undefined, configured: Policies | undefined, publicTraffic: boolean): { policies: Policies; token: string | null } {
  const sourceRules = source?.rules ?? {};
  const configuredRules = configured?.rules ?? {};
  if (Object.keys(configuredRules).some(id => id === APP || id === CONTROL) || Object.hasOwn(configured?.secrets ?? {}, SECRET)) throw new Error("E2B traffic policy names are reserved");
  const rules = { ...sourceRules, ...configuredRules };
  const custom = Object.entries(rules).filter(([id, rule]) => rule?.type === "inbound" && !managed(id, rule));
  if (source?.policies?.length || configured?.policies?.length) throw new Error("E2B traffic requires named ingress policies");
  if (custom.length) {
    if (!publicTraffic) throw new Error("Private traffic conflicts with custom inbound policies; automatic token configuration is unsupported");
    // Do not introduce a second admission path around customer restrictions.
    if (Object.entries(sourceRules).some(([id, rule]) => managed(id, rule))) throw new Error("Custom and managed ingress cannot be combined automatically");
    return { policies: configured ?? {}, token: null };
  }
  if (Object.entries(rules).some(([id, rule]) => rule && !managed(id, rule) && "priority" in rule && [0, 1000000].includes(rule.priority ?? -1))) throw new Error("E2B ingress priorities are reserved");
  const token = publicTraffic ? null : randomBytes(32).toString("base64url");
  const replaced = Object.fromEntries(Object.entries(sourceRules).filter(([id, rule]) => managed(id, rule)).map(([id]) => [id, null]));
  return { token, policies: {
    ...configured,
    rules: { ...configuredRules, ...replaced,
      [APP]: { type: "inbound", priority: 0, action: "allow", auth: "open", match: { ports: PORTS as [number, number][], ...(token ? { headers: { "e2b-traffic-access-token": [`\${secret:${SECRET}}`] } } : {}) } },
      [CONTROL]: { type: "inbound", priority: 1000000, action: "allow", auth: "arker", match: { ports: [49983] } },
    },
    secrets: { ...configured?.secrets, [SECRET]: token },
  } };
}

export async function forkWithTraffic(client: Arker, request: ForkOptions, publicTraffic: boolean): Promise<VM> {
  const source = request.source_vm_id ?? request.source_vm_name;
  const policies = source ? (await client.getVm(source)).policies : undefined;
  const plan = trafficPolicies(policies ?? undefined, request.policies ?? undefined, publicTraffic);
  const vm = await client.fork({ ...request, policies: plan.policies });
  rememberTrafficToken(vm, plan.token);
  return vm;
}
