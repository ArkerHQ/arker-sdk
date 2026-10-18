import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdtemp,
  mkdir,
  readFile,
  writeFile,
  copyFile,
  rm,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";
import { programs } from "../tests/compat-consumer-programs.mjs";

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
  for (const [label, vendor] of [
    ["base", null],
    ["e2b", "e2b"],
    ["blaxel", "@blaxel/core"],
    ["daytona", "@daytona/sdk"],
    ["modal", "modal"],
  ]) {
    const consumer = join(directory, label);
    await mkdir(consumer);
    const dependencies = { "@arker-ai/sdk": `file:../${packed.filename}` };
    dependencies["@types/node"] = JSON.parse(
      await readFile(
        join(root, "node_modules/@types/node/package.json"),
        "utf8",
      ),
    ).version;
    if (vendor)
      dependencies[vendor] = JSON.parse(
        await readFile(
          join(root, "node_modules", vendor, "package.json"),
          "utf8",
        ),
      ).version;
    await writeFile(
      join(consumer, "package.json"),
      JSON.stringify({ private: true, type: "module", dependencies }),
    );
    await copyFile(
      join(root, "tests/compat-consumer-smoke.mjs"),
      join(consumer, "smoke.mjs"),
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
      consumer,
    );
    process.stdout.write(run(process.execPath, ["smoke.mjs", label], consumer));
    const compiler = join(root, "node_modules/typescript/bin/tsc");
    const flags = [
      "--noEmit",
      "--strict",
      "--target",
      "es2022",
      "--module",
      "NodeNext",
      "--moduleResolution",
      "NodeNext",
      "--lib",
      "esnext,dom,dom.iterable",
      "--types",
      "node",
      "--esModuleInterop",
    ];
    for (const [variant, module] of vendor
      ? [
          ["reference", vendor],
          ["shim", `@arker-ai/sdk/${label}`],
        ]
      : [["shim", "@arker-ai/sdk"]]) {
      const files = [];
      for (const extension of ["mts", "cts"]) {
        const filename = `${variant}.${extension}`;
        await writeFile(
          join(consumer, filename),
          programs[label].replaceAll("__IMPORT__", module),
        );
        files.push(filename);
      }
      run(process.execPath, [compiler, ...flags, ...files], consumer);
      console.log(
        `PASS packed SDK ${label} ${variant} ESM/CommonJS strict typecheck`,
      );
    }
  }
  const fingerprint = createHash("sha256")
    .update(await readFile(join(directory, packed.filename)))
    .digest("hex");
  console.log(`Packed SDK SHA-256: ${fingerprint}`);
} finally {
  await rm(directory, { recursive: true });
}
