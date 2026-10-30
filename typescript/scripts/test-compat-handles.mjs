import { build } from "tsup";
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";

const families = ["e2b-", "blaxel-", "daytona-", "modal-", "guest-process-", "compat-"];
const shared = new Set(["daemon-activity", "image-creation-config", "filesystem-contract"]);
const suites = readdirSync(new URL("../tests/", import.meta.url))
  .filter(name => name.endsWith(".test.ts"))
  .map(name => name.slice(0, -".test.ts".length))
  .filter(name => shared.has(name) || families.some(prefix => name.startsWith(prefix)))
  .sort();
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
