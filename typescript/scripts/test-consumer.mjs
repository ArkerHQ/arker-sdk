import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile, copyFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const directory = await mkdtemp(join(tmpdir(), "arker-packed-consumer-"));
const environment = Object.fromEntries(
  Object.entries(process.env).filter(
    ([key]) => !/KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/.test(key),
  ),
);
const run = (command, args, cwd) =>
  execFileSync(command, args, {
    cwd,
    env: environment,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 180_000,
  });
try {
  const [packed] = JSON.parse(
    run(
      "npm",
      ["pack", "--ignore-scripts", "--pack-destination", directory, "--json"],
      root,
    ),
  );
  assert.equal(packed.filename, basename(packed.filename));
  const dependencies = { "@arker-ai/sdk": `file:./${packed.filename}` };
  for (const vendor of ["e2b", "@blaxel/core", "@daytona/sdk"]) {
    dependencies[vendor] = JSON.parse(
      await readFile(
        join(root, "node_modules", vendor, "package.json"),
        "utf8",
      ),
    ).version;
  }
  await writeFile(
    join(directory, "package.json"),
    JSON.stringify({ private: true, type: "module", dependencies }),
  );
  await copyFile(
    join(root, "tests/compat-consumer-smoke.mjs"),
    join(directory, "smoke.mjs"),
  );
  run(
    "npm",
    [
      "install",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      "--registry=https://registry.npmjs.org",
    ],
    directory,
  );
  process.stdout.write(run(process.execPath, ["smoke.mjs"], directory));
  const fingerprint = createHash("sha256")
    .update(await readFile(join(directory, packed.filename)))
    .digest("hex");
  console.log(`Packed SDK SHA-256: ${fingerprint}`);
} finally {
  await rm(directory, { recursive: true });
}
