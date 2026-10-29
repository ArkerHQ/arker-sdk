/** Bootstrap native envd through secret sync; never changes ingress policies. */
import { randomBytes, randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";
import type { VM } from "../index.js";
import type { EnvdDescriptor } from "./e2b-data-plane.js";
import { stageEnvdArtifact } from "./e2b-artifact.js";
import { guestScript, manifest } from "./e2b-envd-guest.generated.js";

const scriptPath = "/root/.arker-envd-bootstrap.py";
const quote = (value: string) => `'${value.replace(/'/g, `'"'"'`)}'`;
export interface EnvdReadiness {
  readonly ready: true; readonly reused: boolean; readonly port: number;
  readonly state_path: string; readonly version: string; readonly arch: "amd64" | "arm64";
}
interface Prepared {
  root_path: string; request_directory: string; state_path: string;
  existing_state_path: string | null; arch: "amd64" | "arm64";
}
async function guestJson(vm: VM, operation: "prepare" | "verify" | "ensure", argument: string) {
  const result = await vm.run(`python3 ${quote(scriptPath)} ${operation} ${quote(argument)}`,
    { timeout: 120, end_after_all_subprocesses: false });
  if (result.exitCode !== 0) throw new Error("Guest bootstrap failed; no descriptor was produced");
  return JSON.parse(result.stdout);
}

export function validateEnvdEnvs(envs: Record<string, string> | undefined): void {
  if (envs !== undefined && (!envs || typeof envs !== "object" || Array.isArray(envs) || Object.values(envs).some(value => typeof value !== "string"))) {
    throw new TypeError("envs must be a string-to-string object");
  }
}

/** Approved envd-free sources or same-trust reconnect/forks only. No platform key enters the guest. */
export async function bootstrapEnvd(vm: VM, options: { token?: string; cacheDirectory?: string; envs?: Record<string, string> } = {}):
Promise<readonly [EnvdDescriptor, EnvdReadiness]> {
  validateEnvdEnvs(options.envs);
  const policyVM = await vm.refresh();
  // Port exposure unions rules before ordered request filtering. A higher
  // priority private rule cannot establish an owner-only ingress boundary.
  for (const rule of Object.values(policyVM.policies?.rules ?? {})) {
    if (rule.type !== "inbound" || rule.action === "deny" || rule.auth === undefined || rule.auth === "arker") continue;
    const ports = rule.match?.ports;
    const coversEnvd = ports === undefined || ports.some(port => typeof port === "number"
      ? port === 49983 : Math.min(...port) <= 49983 && Math.max(...port) >= 49983);
    if (coversEnvd) throw new Error("Native envd requires owner-authenticated ingress on port 49983; incompatible VM policy");
  }
  await vm.sync(scriptPath, guestScript, { isSecret: true });
  const prepared: Prepared = await guestJson(vm, "prepare", vm.id);
  let token = options.token;
  if (token === undefined) {
    let source = prepared.existing_state_path;
    if (source === null) {
      const found = await vm.run(`test -f ${quote(prepared.state_path)}`, { timeout: 10 });
      if (found.exitCode === 0) source = prepared.state_path;
      else if (found.exitCode !== 1) throw new Error("Cannot inspect envd state");
    }
    token = source === null ? randomBytes(32).toString("base64url")
      : JSON.parse(new TextDecoder().decode(await vm.sync(source))).token;
  }
  if (typeof token !== "string" || !/^[A-Za-z0-9_-]{43,128}$/.test(token)) throw new Error("Invalid envd token");
  const root = prepared.root_path;
  await vm.sync(`${root}/_guest_e2b_envd_manifest.json`, JSON.stringify(manifest), { isSecret: true });
  const verified = await guestJson(vm, "verify", vm.id);
  if (typeof verified.verified !== "boolean" || verified.arch !== prepared.arch) {
    throw new Error("Guest artifact verification returned an invalid response");
  }
  const candidate = verified.verified ? null : await stageEnvdArtifact(vm, root, manifest.artifacts[prepared.arch],
    options.cacheDirectory ?? join(homedir(), ".cache", "arker", "envd"));
  const request = `${prepared.request_directory}/${randomUUID()}.json`;
  await vm.sync(request, JSON.stringify({ vm_id: vm.id, token, artifact_path: candidate, offline: true, ...(options.envs !== undefined ? { envs: options.envs } : {}) }), { isSecret: true });
  const ready: EnvdReadiness = await guestJson(vm, "ensure", request);
  if (ready.ready !== true || ready.port !== 49983 || ready.version !== manifest.version) throw new Error("Guest bootstrap did not establish readiness");
  const current = await vm.refresh();
  if (!current.hostname?.includes(".")) throw new Error("Missing VM endpoint");
  const dot = current.hostname.indexOf(".");
  const endpoint = `https://${current.hostname.slice(0, dot)}-49983.${current.hostname.slice(dot + 1)}`;
  return [Object.freeze({ endpoint, envd_version: ready.version, envd_access_token: token }), Object.freeze(ready)];
}
