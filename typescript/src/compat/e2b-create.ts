import type { components } from "../generated/api-types.js";
import { isIP } from "node:net";
import { createHash } from "node:crypto";
import { InvalidArgumentError, SandboxError } from "e2b";

export function creationKey(value: unknown): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !/^[a-z0-9-]{1,255}$/.test(value)) {
    throw new InvalidArgumentError("Idempotency key must contain 1-255 lowercase letters, digits or hyphens");
  }
  return value;
}

export function canonicalHash(value: unknown): string {
  const normalize = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(normalize);
    if (node && typeof node === "object") return Object.fromEntries(Object.entries(node)
      .filter(([, v]) => v !== undefined).sort(([a], [b]) => Buffer.compare(Buffer.from(a), Buffer.from(b)))
      .map(([k, v]) => [k, normalize(v)]));
    return node;
  };
  return createHash("sha256").update(JSON.stringify(normalize(value))).digest("hex");
}

export function resolveNetwork(network: { allowPublicTraffic?: boolean; allowOut?: unknown; denyOut?: unknown; dnsServers?: unknown } | undefined) {
  if (network === undefined) return undefined;
  const selectors = (value: unknown): string[] | undefined => {
    if (typeof value === "function") value = value({ allTraffic: "0.0.0.0/0", rules: new Map() });
    if (value === undefined) return undefined;
    if (!Array.isArray(value) || value.some(item => typeof item !== "string")) throw new TypeError("Outbound selectors must be strings");
    return value;
  };
  return { ...network, ...(network.allowOut !== undefined ? {allowOut: selectors(network.allowOut)} : {}),
    ...(network.denyOut !== undefined ? {denyOut: selectors(network.denyOut)} : {}) };
}

export function outboundPolicy(network: ReturnType<typeof resolveNetwork>) {
  if (network === undefined) return undefined;
  const deny = network.denyOut as string[] | undefined ?? [];
  const allowed = network.allowOut as string[] | undefined;
  const dns = network.dnsServers ?? ["8.8.8.8"];
  if (!Array.isArray(dns) || dns.length > 10 || dns.some(value => typeof value !== "string" || isIP(value) !== 4)) {
    throw new TypeError("dnsServers must contain at most ten IPv4 addresses");
  }
  if (deny.length && (deny.length !== 1 || deny[0] !== "0.0.0.0/0")) {
    throw new TypeError("Only deny-all with explicit outbound exceptions is supported");
  }
  if (allowed === undefined && deny.length === 0) return undefined;
  const hosts: string[] = [], ips: string[] = [];
  for (const entry of allowed ?? []) {
    const [address, prefix, extra] = entry.split("/");
    const version = isIP(address!);
    if (version && extra === undefined && (prefix === undefined || (/^\d+$/.test(prefix) && Number(prefix) <= (version === 4 ? 32 : 128)))) ips.push(entry);
    else if (/^[A-Za-z0-9*](?:[A-Za-z0-9*.-]*[A-Za-z0-9])?$/.test(entry)) hosts.push(entry);
    else throw new TypeError("Invalid outbound host selector");
  }
  if (hosts.length && !deny.length) {
    const error = new SandboxError("Domain allowlists require denyOut to include 0.0.0.0/0");
    error.statusCode = 400;
    throw error;
  }
  if (!deny.length) return undefined;
  if (hosts.length) ips.push(...dns);
  const rule = { type: "outbound_guard" as const, allow: [
    ...(hosts.length ? [{ hosts }] : []), ...(ips.length ? [{ ips }] : []),
  ] };
  const rules: Record<string, components["schemas"]["OutboundGuardRule"]> = {
    [guardName(rule)]: rule,
  };
  if (hosts.length) {
    // A separate IP/port floor constrains the hostname guard's implicit DNS.
    const dnsRule = { type: "outbound_guard" as const, allow: [
      { ports: [[1, 52], [54, 65535]] }, ...(ips.length ? [{ ips }] : []),
    ] };
    rules[guardName(dnsRule)] = dnsRule;
  }
  return { rules };
}

export function guardName(rule: unknown): string {
  // Policy IDs have a 64-character wire limit, including this prefix.
  return "e2b-egress-" + canonicalHash(rule).slice(0, 53);
}


export function creationIntent(values: Record<string, unknown>, mounts?: Record<string, string | { volumeId: string }>): string {
  const names = ["envs", "metadata", "network"] as const;
  const normalize = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(normalize);
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) =>
      [key.replace(/[A-Z]/g, letter => "_" + letter.toLowerCase()), normalize(item)]));
    return value;
  };
  const intent: Record<string, unknown> = {
    template: values.templateId ?? values.template ?? null,
    timeout: Math.ceil(Number(values.timeoutMs ?? values.timeout ?? 300_000) / 1000),
    internet: values.allowInternetAccess ?? null,
    envs: {}, metadata: {}, network: {},
  };
  for (const key of names) if (values[key] != null) intent[key] = key === "network" ? normalize(values[key]) : values[key];
  if (mounts && Object.keys(mounts).length) intent.volume_mounts = Object.fromEntries(Object.entries(mounts).map(([path, volume]) => [path, typeof volume === "string" ? volume : volume.volumeId]));
  return canonicalHash(intent);
}
