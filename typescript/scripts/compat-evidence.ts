import { createHash } from "node:crypto";
import { appendFile, readdir, readFile, realpath } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

if (process.env.COMPAT_COVERAGE_EVIDENCE && !process.env.COMPAT_COVERAGE_RUN?.trim()) {
  throw new Error("COMPAT_COVERAGE_RUN is required when recording evidence");
}

async function dependencyManifest(name: string, directory: string): Promise<string> {
  const require = createRequire(resolve(await realpath(directory), "package.json"));
  let candidate = dirname(require.resolve(name));
  while (true) {
    const manifest = join(candidate, "package.json");
    try {
      if (JSON.parse(await readFile(manifest, "utf8")).name === name) return manifest;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    const parent = dirname(candidate);
    if (parent === candidate) throw new Error(`Cannot locate manifest for ${name}`);
    candidate = parent;
  }
}

export function runtimeInfo() {
  return { node: process.versions.node, bun: process.versions.bun ?? null, platform: process.platform, architecture: process.arch };
}

export async function implementationFingerprint(read: (path: string) => Promise<Uint8Array> = readFile): Promise<string> {
  const root = fileURLToPath(new URL("../src", import.meta.url));
  const paths: string[] = [
    resolve(root, "../../bun.lock"), resolve(root, "../package.json"),
    resolve(root, "../../python/src/arker/_guest_process.py"),
    resolve(root, "../../python/src/arker/_linux_signals.json"),
    resolve(root, "../../python/src/arker/_guest_e2b_envd_manifest.json"),
    resolve(root, "../../compatibility/http-service.py"),
    resolve(root, "../../compatibility/api-scope.json"),
    resolve(root, "../scripts/compat-evidence.ts"), resolve(root, "../scripts/compat-coverage.ts"),
  ];
  for (const name of ["computesdk", "@computesdk/provider"]) {
    const directory = resolve(root, "../node_modules", name);
    const manifest = resolve(directory, "package.json");
    const pkg = JSON.parse(await readFile(manifest, "utf8"));
    paths.push(manifest);
    for (const entry of new Set([pkg.main, pkg.module])) {
      if (typeof entry === "string") paths.push(resolve(directory, entry));
    }
  }
  const tests = resolve(root, "../tests");
  for (const name of await readdir(tests)) {
    if (name.startsWith("compat") && name.endsWith(".ts")) paths.push(join(tests, name));
  }
  async function visit(directory: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await visit(path);
      else if (path.endsWith(".ts")) paths.push(path);
    }
  }
  await visit(root);
  const hash = createHash("sha256");
  hash.update(JSON.stringify(runtimeInfo()) + "\0");
  for (const path of paths.sort()) {
    hash.update(relative(resolve(root, "../.."), path) + "\0");
    hash.update(await read(path));
    hash.update("\0");
  }
  return hash.digest("hex");
}

export interface ModalCleanupReceipt {
  protocol: "modal-terminal-v1";
  resources: string[];
  observations: Array<{ id: string; operation: "Sandbox.from_id.poll"; returncode: number }>;
}

// These are trusted harness observations, not signed provider proofs.
export function cleanupObserved(item: any): boolean {
  const receipt = item.cleanup;
  return item.provider === "modal" && receipt?.protocol === "modal-terminal-v1" &&
    Array.isArray(receipt.resources) && receipt.resources.length > 0 &&
    receipt.resources.every((id: unknown) => typeof id === "string" && id.length > 0) &&
    new Set(receipt.resources).size === receipt.resources.length &&
    Array.isArray(receipt.observations) && receipt.observations.length === receipt.resources.length &&
    receipt.observations.every((observation: any, index: number) => observation?.id === receipt.resources[index] &&
      observation.operation === "Sandbox.from_id.poll" && Number.isSafeInteger(observation.returncode));
}

export class ModalCleanup {
  private resources: string[] = [];
  private observations = new Map<string, ModalCleanupReceipt["observations"][number]>();

  allocated(id: string): void {
    if (!id || this.resources.includes(id)) throw new Error("Expected a new sandbox identifier");
    this.resources.push(id);
  }

  async observe(id: string, lookup: (id: string) => Promise<{ sandboxId: string; poll(): Promise<number | null> }>): Promise<void> {
    if (!this.resources.includes(id)) throw new Error("Sandbox was not enrolled");
    this.observations.delete(id);
    const sandbox = await lookup(id);
    if (sandbox.sandboxId !== id) throw new Error("Cleanup readback returned a different sandbox");
    const returncode = await sandbox.poll();
    if (!Number.isSafeInteger(returncode)) throw new Error("Sandbox termination has not been observed");
    this.observations.set(id, { id, operation: "Sandbox.from_id.poll", returncode: returncode! });
  }

  receipt(): ModalCleanupReceipt {
    return { protocol: "modal-terminal-v1", resources: [...this.resources],
      observations: this.resources.flatMap(id => { const observation = this.observations.get(id); return observation ? [{ ...observation }] : []; }) };
  }
}

export function verifiedScenarios(api: string, evidence: any[], version: string, fingerprint: string): string[] {
  const groups = new Map<string, { routes: Set<string>; failed: boolean }>();
  for (const item of evidence) {
    if (item.implementation_fingerprint !== fingerprint || item.vendor_version !== version) continue;
    if (![item.run, item.scenario].every((value) => typeof value === "string" && value.trim()) ||
      !["reference", "native", "arker"].includes(item.route) ||
      !["passed", "failed"].includes(item.outcome) || !Array.isArray(item.apis) ||
      !item.apis.every((api: unknown) => typeof api === "string" && api.trim())) {
      throw new Error("Invalid compatibility evidence");
    }
    const key = JSON.stringify([item.run, item.scenario]);
    const group = groups.get(key) ?? { routes: new Set<string>(), failed: false };
    if (item.outcome === "passed" && cleanupObserved(item) && item.apis.includes(api)) group.routes.add(item.route);
    else if (item.outcome !== "passed") group.failed = true;
    groups.set(key, group);
  }
  return [...groups].filter(([, group]) => !group.failed && ["reference", "native", "arker"].every((route) => group.routes.has(route)))
    .map(([key]) => (JSON.parse(key) as string[]).join(":")).sort();
}

export function captureEvidenceSnapshot(readFingerprint = implementationFingerprint) {
  const started = readFingerprint();
  return async () => {
    const fingerprint = await started;
    return { fingerprint, changed: fingerprint !== await readFingerprint() };
  };
}

const finishSnapshot = captureEvidenceSnapshot();
const packages = { blaxel: "@blaxel/core", e2b: "e2b", modal: "modal", daytona: "@daytona/sdk" };
async function vendorVersion(provider: keyof typeof packages): Promise<string> {
  return JSON.parse(await readFile(new URL(`../node_modules/${packages[provider]}/package.json`, import.meta.url), "utf8")).version;
}
const initialVersions = Promise.all((Object.keys(packages) as Array<keyof typeof packages>).map(async (provider) => (
  [provider, await vendorVersion(provider)] as const
))).then(Object.fromEntries);

export async function recordEvidence(provider: "blaxel" | "e2b" | "modal" | "daytona", route: string, scenario: string, apis: string[], outcome: string, signatures: Record<string, string[]> = {}, cleanup?: ModalCleanupReceipt): Promise<void> {
  const output = process.env.COMPAT_COVERAGE_EVIDENCE;
  if (!output) return;
  if (!process.env.COMPAT_COVERAGE_RUN) throw new Error("COMPAT_COVERAGE_RUN is required when recording evidence");
  if (!signatures || Array.isArray(signatures) || typeof signatures !== "object" ||
    !Object.entries(signatures).every(([api, values]) => apis.includes(api) && Array.isArray(values) && values.length > 0 &&
      values.every(value => typeof value === "string" && value.trim()))) {
    throw new Error("Signature attestations must name exercised APIs and nonempty signatures");
  }
  const version = await vendorVersion(provider);
  if (route === "native") {
    const providerDirectory = fileURLToPath(new URL("../src/compat", import.meta.url));
    const nativeManifest = await dependencyManifest(packages[provider], providerDirectory);
    const nativeVersion = JSON.parse(await readFile(nativeManifest, "utf8")).version;
    if (nativeVersion !== version) throw new Error(`Reference ${provider} ${version} differs from provider dependency ${nativeVersion}`);
  }
  const snapshot = await finishSnapshot();
  const fingerprint = snapshot.fingerprint;
  const versions = await initialVersions;
  const changed = snapshot.changed || versions[provider] !== version;
  await appendFile(output, JSON.stringify({
    provider, cleanup,
    run: process.env.COMPAT_COVERAGE_RUN,
    scenario, route, outcome: changed ? "failed" : outcome,
    implementation_fingerprint: fingerprint,
    vendor_version: versions[provider],
    apis: apis.map((api) => `typescript:${provider}:async:${api}`),
    signatures: Object.fromEntries(Object.entries(signatures).map(([api, values]) => [`typescript:${provider}:async:${api}`, values])),
  }) + "\n", { mode: 0o600 });
  if (changed) throw new Error("Implementation changed during the live compatibility scenario");
}
