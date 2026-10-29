import { VM, type Arker, type ForkOptions } from "../index.js";
import { parseDockerfile, type Step } from "../buildSpec.js";
import { validateHttpError } from "../generated/error-validators.js";
import { shellQuote } from "./common.js";

export const BUILD_POLICY = "computesdk-modal-build-lifetime";
export const IMAGE_WORKDIR = "computesdk-modal-image-workdir";
const BUILD_INGRESS = "computesdk-modal-build-ingress";
export const CONFIG_PATH = "/etc/arker/image-config.json";
const PROFILE_PATH = "/etc/profile.d/99-arker-image.sh";
type Resolved = { env: [string, string][]; user: string | null; workdir: string | null; cmd: string | null; entrypoint: string | null };
const plans = new WeakMap<object, Step[]>();

export function preparedSteps(commands: string[]): Step[] {
  if (!Array.isArray(commands) || commands.some(value => typeof value !== "string")) throw new TypeError("Image commands must be strings");
  const steps = parseDockerfile(`FROM prepared-base\nSHELL ["/bin/sh", "-c"]\n${commands.join("\n")}`).steps;
  if (steps.some(step => !["run", "cmd", "entrypoint"].includes(step.kind))) throw new Error("Prepared registry recipes currently support RUN, CMD and ENTRYPOINT");
  if (!steps.some(step => step.kind === "cmd") || !steps.some(step => step.kind === "entrypoint")) throw new Error("Prepared recipes must explicitly set both CMD and ENTRYPOINT");
  return steps;
}

export function preparedMetadata(metadata: object, steps: Step[]): object {
  plans.set(metadata, steps);
  return metadata;
}

export function imageMetadata(value: Resolved): [string, string] {
  const text = (item: unknown): item is string => typeof item === "string" && !item.includes("\0") && Buffer.byteLength(item) <= 32768;
  if (!Array.isArray(value.env) || value.env.length > 256 || value.env.some(pair => !Array.isArray(pair) || pair.length !== 2 || !text(pair[0]) || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(pair[0]) || !text(pair[1]))) throw new Error("Invalid prepared image environment");
  for (const key of ["user", "workdir", "cmd", "entrypoint"] as const) if (value[key] !== null && !text(value[key])) throw new Error("Invalid prepared image configuration");
  if (value.user !== null && !/^[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?$/.test(value.user)) throw new Error("Invalid prepared image user");
  if (value.workdir !== null && !value.workdir.startsWith("/")) throw new Error("Prepared image workdir must be absolute");
  const encoded = JSON.stringify(value);
  if (Buffer.byteLength(encoded) > 1024 * 1024) throw new Error("Prepared image configuration exceeds 1 MiB");
  const profile = "# Resolved image environment and working directory.\n" + value.env.map(([key, entry]) => `export ${key}=${shellQuote(entry)}\n`).join("") + (value.workdir === null ? "" : `cd ${shellQuote(value.workdir)} 2>/dev/null || true\n`);
  return [encoded, profile];
}

function recovery(error: unknown, client: Arker, baseUrl?: string): VM | undefined {
  const body = error && typeof error === "object" && "body" in error ? error.body : undefined;
  if (validateHttpError(body) && body.recovery?.context?.vm_id) return new VM(client, body.recovery.context.vm_id, baseUrl);
}

async function dispose(vm: VM): Promise<void> {
  try { await vm.delete(); }
  catch (error) { throw Object.assign(new Error(`Prepared image cleanup failed for VM ${vm.id}`, { cause: error }), { vmId: vm.id }); }
}

export async function finishPrepared(builder: VM, encoded: [string, string], final: Pick<ForkOptions, "policies" | "metadata" | "name">, signal?: AbortSignal): Promise<VM> {
  const owned = [builder];
  const fork = async (source: VM, request: NonNullable<Parameters<VM["fork"]>[0]>) => {
    signal?.throwIfAborted();
    let child: VM;
    try { child = await source.fork(request); }
    catch (error) { const pending = recovery(error, builder._client, builder.baseUrl); if (pending && owned.every(vm => vm.id !== pending.id)) owned.push(pending); throw error; }
    owned.push(child);
    signal?.throwIfAborted();
    return child;
  };
  try {
    const policies = structuredClone(final.policies ?? {});
    if ("policies" in policies || BUILD_POLICY in (policies.rules ?? {})) throw new Error("Prepared images require named policies without reserved build rules");
    policies.rules = { ...policies.rules, [BUILD_POLICY]: null };
    signal?.throwIfAborted();
    await builder.sync(PROFILE_PATH, encoded[1]);
    signal?.throwIfAborted();
    await builder.sync(CONFIG_PATH, encoded[0]);
    const capture = await fork(builder, { immutable: true, policies: { rules: {
      [BUILD_POLICY]: { type: "schedule", after_created: 300, action: "delete" },
    } } });
    const childRequest = { layers: ["disk"] as "disk"[], start_image: true, policies,
      ...(final.metadata === undefined ? {} : { metadata: final.metadata }), ...(final.name === undefined ? {} : { name: final.name }) };
    const child = await fork(capture, childRequest);
    for (const resource of owned.slice(0, -1).reverse()) { await dispose(resource); owned.splice(owned.indexOf(resource), 1); }
    signal?.throwIfAborted();
    return child;
  } catch (error) {
    const failures: unknown[] = [];
    for (const resource of owned.reverse()) { try { await dispose(resource); } catch (cleanup) { failures.push(cleanup); } }
    if (failures.length) throw new AggregateError([error, ...failures], "Prepared image creation and cleanup failed");
    throw error;
  }
}

export async function createPrepared(client: Arker, request: ForkOptions, steps: Step[], buildTimeout = 900): Promise<VM> {
  const policies = structuredClone(request.policies ?? {});
  if ("policies" in policies || policies.secrets || BUILD_POLICY in (policies.rules ?? {})) throw new Error("Prepared builds require named policies without policy secrets or reserved build rules");
  const rules = policies.rules ?? {};
  if (BUILD_INGRESS in rules || "computesdk-modal-prepared-ingress" in rules || Object.values(rules).some(rule => rule?.type === "inbound")) throw new Error("Prepared builds cannot yet preserve custom inbound rules");
  if (request.image_config !== undefined) throw new Error("Prepared recipes cannot combine an explicit image_config override");
  if (!Number.isInteger(buildTimeout) || buildTimeout <= 0) throw new Error("Prepared image build timeout must be positive");
  const buildRules = { ...rules };
  delete buildRules["computesdk-modal-lifetime"];
  buildRules[BUILD_POLICY] = { type: "schedule", after_created: buildTimeout, action: "delete" };
  buildRules[BUILD_INGRESS] = { type: "inbound", priority: 0, action: "deny" };
  const { name: _name, metadata: _metadata, policies: _policies, ...initial } = request;
  const deadline = performance.now() + buildTimeout * 1000;
  let builder: VM;
  try { builder = await client.fork({ ...initial, image_config: { cmd: [], entrypoint: [] }, policies: { ...policies, rules: buildRules } }); }
  catch (error) {
    const pending = recovery(error, client);
    if (pending) { try { await dispose(pending); } catch (cleanup) { throw new AggregateError([error, cleanup], "Prepared allocation and cleanup failed"); } }
    throw error;
  }
  let transferred = false;
  try {
    const raw = await builder.sync(CONFIG_PATH);
    if (raw.byteLength > 1024 * 1024) throw new Error("Prepared image configuration exceeds 1 MiB");
    const inherited = JSON.parse(new TextDecoder().decode(raw));
    const resolved: Resolved = { env: inherited.env, user: inherited.user ?? null, workdir: inherited.workdir ?? null, cmd: null, entrypoint: null };
    imageMetadata(resolved);
    for (const step of steps) {
      if (step.kind === "run") {
        const remaining = Math.floor((deadline - performance.now()) / 1000);
        if (remaining <= 0) throw new Error("Prepared image build deadline exceeded");
        const session = await builder.createSession();
        try {
          const result = await builder.run(step.command, { session_id: session.session_id, timeout: remaining });
          if (result.type !== "completed" || result.exitCode !== 0) throw new Error("Prepared image build command failed");
        } catch (error) {
          try { await builder.deleteSession(session.session_id); }
          catch (cleanup) { throw new AggregateError([error, cleanup], "Build command and session cleanup failed"); }
          throw error;
        }
        await builder.deleteSession(session.session_id);
      } else if (step.kind === "cmd" || step.kind === "entrypoint") resolved[step.kind] = step.value || null;
    }
    const encoded = imageMetadata(resolved);
    policies.rules = { ...rules, [BUILD_INGRESS]: null,
      "computesdk-modal-prepared-ingress": { type: "inbound", priority: 0, action: "allow", auth: "arker" },
    };
    transferred = true;
    return await finishPrepared(builder, encoded, { ...request, policies, metadata: { ...request.metadata, [IMAGE_WORKDIR]: resolved.workdir ?? "/" } });
  } catch (error) {
    if (!transferred) { try { await dispose(builder); } catch (cleanup) { throw new AggregateError([error, cleanup], "Prepared build and cleanup failed"); } }
    throw error;
  }
}

export function preparedFork(client: Arker, request: ForkOptions): Promise<VM> {
  const steps = request.metadata && typeof request.metadata === "object" ? plans.get(request.metadata) : undefined;
  return steps ? createPrepared(client, request, steps) : client.fork(request);
}
