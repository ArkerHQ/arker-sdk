import { createHash } from "node:crypto";
import { appendFile, readdir, readFile, realpath } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

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

export async function implementationFingerprint(read: (path: string) => Promise<Uint8Array> = readFile): Promise<string> {
  const root = fileURLToPath(new URL("../src", import.meta.url));
  const paths: string[] = [
    resolve(root, "../../bun.lock"), resolve(root, "../package.json"),
    resolve(root, "../../python/src/arker/_guest_process.py"),
    resolve(root, "../scripts/compat-evidence.ts"), resolve(root, "../scripts/compat-coverage.ts"),
  ];
  for (const name of ["computesdk", "@computesdk/e2b", "@computesdk/daytona", "@computesdk/modal", "@computesdk/blaxel"]) {
    const directory = resolve(root, "../node_modules", name);
    const manifest = resolve(directory, "package.json");
    const pkg = JSON.parse(await readFile(manifest, "utf8"));
    paths.push(manifest);
    for (const entry of new Set([pkg.main, pkg.module])) {
      if (typeof entry === "string") paths.push(resolve(directory, entry));
    }
    if (name.startsWith("@computesdk/")) {
      const manifestPath = await dependencyManifest("@computesdk/provider", directory);
      const dependencyDirectory = dirname(manifestPath);
      const dependency = JSON.parse(await readFile(manifestPath, "utf8"));
      paths.push(manifestPath);
      for (const entry of new Set([dependency.main, dependency.module])) {
        if (typeof entry === "string") paths.push(resolve(dependencyDirectory, entry));
      }
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
  for (const path of paths.sort()) {
    hash.update(relative(resolve(root, "../.."), path) + "\0");
    hash.update(await read(path));
    hash.update("\0");
  }
  return hash.digest("hex");
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
    if (item.outcome === "passed" && item.apis.includes(api)) group.routes.add(item.route);
    else if (item.outcome !== "passed") group.failed = true;
    groups.set(key, group);
  }
  return [...groups].filter(([, group]) => !group.failed && ["reference", "native", "arker"].every((route) => group.routes.has(route)))
    .map(([key]) => (JSON.parse(key) as string[]).join(":")).sort();
}

export async function recordEvidence(provider: "blaxel" | "e2b" | "modal" | "daytona", route: string, scenario: string, apis: string[], outcome: string): Promise<void> {
  const output = process.env.COMPAT_COVERAGE_EVIDENCE;
  if (!output) return;
  if (!process.env.COMPAT_COVERAGE_RUN) throw new Error("COMPAT_COVERAGE_RUN is required when recording evidence");
  const packages = { blaxel: "@blaxel/core", e2b: "e2b", modal: "modal", daytona: "@daytona/sdk" };
  const version = JSON.parse(await readFile(new URL(`../node_modules/${packages[provider]}/package.json`, import.meta.url), "utf8")).version;
  if (route === "native") {
    const providerDirectory = fileURLToPath(new URL(`../node_modules/@computesdk/${provider}`, import.meta.url));
    const nativeManifest = await dependencyManifest(packages[provider], providerDirectory);
    const nativeVersion = JSON.parse(await readFile(nativeManifest, "utf8")).version;
    if (nativeVersion !== version) throw new Error(`Reference ${provider} ${version} differs from provider dependency ${nativeVersion}`);
  }
  await appendFile(output, JSON.stringify({
    provider,
    run: process.env.COMPAT_COVERAGE_RUN,
    scenario, route, outcome,
    implementation_fingerprint: await implementationFingerprint(),
    vendor_version: version,
    apis: apis.map((api) => `typescript:${provider}:async:${api}`),
  }) + "\n", { mode: 0o600 });
}
