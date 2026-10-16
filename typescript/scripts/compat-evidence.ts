import { createHash } from "node:crypto";
import { appendFile, readdir, readFile } from "node:fs/promises";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export async function implementationFingerprint(): Promise<string> {
  const root = fileURLToPath(new URL("../src", import.meta.url));
  const paths: string[] = [
    resolve(root, "../../bun.lock"), resolve(root, "../package.json"),
    resolve(root, "../../python/src/arker/_guest_process.py"),
  ];
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
    hash.update(await readFile(path));
    hash.update("\0");
  }
  return hash.digest("hex");
}

export function verifiedScenarios(api: string, evidence: any[], version: string, fingerprint: string): string[] {
  const groups = new Map<string, { routes: Set<string>; failed: boolean }>();
  for (const item of evidence) {
    if (item.implementation_fingerprint !== fingerprint || item.vendor_version !== version) continue;
    const key = `${item.run}:${item.scenario}`;
    const group = groups.get(key) ?? { routes: new Set<string>(), failed: false };
    if (item.outcome === "passed" && item.apis.includes(api)) group.routes.add(item.route);
    else if (item.outcome !== "passed") group.failed = true;
    groups.set(key, group);
  }
  return [...groups].filter(([, group]) => !group.failed && ["reference", "native", "arker"].every((route) => group.routes.has(route)))
    .map(([key]) => key).sort();
}

export async function recordEvidence(provider: "blaxel" | "e2b" | "modal" | "daytona", route: string, scenario: string, apis: string[], outcome: string): Promise<void> {
  const output = process.env.COMPAT_COVERAGE_EVIDENCE;
  if (!output) return;
  if (!process.env.COMPAT_COVERAGE_RUN) throw new Error("COMPAT_COVERAGE_RUN is required when recording evidence");
  const packages = { blaxel: "@blaxel/core", e2b: "e2b", modal: "modal", daytona: "@daytona/sdk" };
  const version = JSON.parse(await readFile(new URL(`../node_modules/${packages[provider]}/package.json`, import.meta.url), "utf8")).version;
  await appendFile(output, JSON.stringify({
    provider,
    run: process.env.COMPAT_COVERAGE_RUN,
    scenario, route, outcome,
    implementation_fingerprint: await implementationFingerprint(),
    vendor_version: version,
    apis: apis.map((api) => `typescript:${provider}:async:${api}`),
  }) + "\n", { mode: 0o600 });
}
