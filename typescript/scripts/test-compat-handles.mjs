import { build } from "tsup";
import { spawnSync } from "node:child_process";

const suites = [
  "e2b-inventory", "e2b-metadata", "e2b-network", "e2b-artifact", "e2b-bootstrap", "e2b-data-plane", "e2b-file-streams",
  "e2b-transport", "e2b-lifecycle-extended", "e2b-paused-admission", "e2b-mounts", "e2b-urls", "e2b-urls-unsupported",
  "e2b-lifecycle", "e2b-filesystem", "e2b-staging", "compat-live-router", "compat-trace",
  "blaxel-provider", "blaxel-data-plane", "blaxel-facade", "blaxel-network", "blaxel-packages", "daemon-activity", "image-creation-config", "blaxel-literal-paths", "daytona-provider", "daytona-filesystem", "daytona-volumes", "modal-provider",
  "modal-sandbox", "modal-recipes", "modal-prepared", "modal-terminal", "modal-lifetime", "modal-command", "modal-errors", "modal-filesystem", "filesystem-contract", "guest-process-input",
];
const outDir = "node_modules/.cache/compat-tests";
await build({
  entry: suites.filter(name => name.startsWith("e2b-")).map(name => `tests/${name}.test.ts`), outDir,
  format: ["cjs"], platform: "node", target: "node18", clean: true,
  splitting: false, removeNodeProtocol: false, silent: true,
  external: ["e2b", "@blaxel/core", "@daytona/sdk", "modal"],
});
const otherSuites = suites.filter(name => !name.startsWith("e2b-"));
const bundled = spawnSync("bun", ["build", ...otherSuites.map(name => `tests/${name}.test.ts`),
  "--target=node", "--external=e2b", "--external=@blaxel/core", "--external=@daytona/sdk", "--external=modal",
  `--outdir=${outDir}`, "--root=tests", "--format=cjs"], { stdio: "inherit" });
if (bundled.error) throw bundled.error;
if (bundled.status !== 0) process.exit(bundled.status ?? 1);
for (const suite of suites) {
  const result = spawnSync(process.execPath, [`${outDir}/${suite}.test.${suite.startsWith("e2b-") ? "cjs" : "js"}`], { stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
