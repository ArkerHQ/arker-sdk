import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FileType, Sandbox as Native } from "e2b";
import { Sandbox as Shim } from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { traceVendor } from "./compat-trace.js";

type SandboxConstructor = { create(): Promise<Native | Shim> };

async function program(Sandbox: SandboxConstructor): Promise<void> {
  const sandbox = await Sandbox.create();
  const root = "/tmp/compat-e2b-files";
  const relative = "compat-e2b-relative";
  try {
    await sandbox.commands.run(`rm -rf ${root} /home/user/${relative}`);
    await sandbox.files.makeDir(root);
    const text = await sandbox.files.write(`${root}/text.txt`, "hello\n");
    const binary = await sandbox.files.write(
      `${root}/binary.bin`,
      Uint8Array.of(0, 255, 16).buffer,
    );
    const relativeInfo = await sandbox.files.write(
      `${relative}/cwd.txt`,
      "cwd",
    );
    const batch = await sandbox.files.writeFiles([
      { path: `${root}/batch-a.txt`, data: "a" },
      { path: `${root}/batch-b.bin`, data: Uint8Array.of(1, 2).buffer },
    ]);
    assert.equal(text.path, `${root}/text.txt`);
    assert.equal(binary.path, `${root}/binary.bin`);
    assert.equal(relativeInfo.path, `/home/user/${relative}/cwd.txt`);
    assert.deepEqual(
      batch.map((entry) => entry.path),
      [`${root}/batch-a.txt`, `${root}/batch-b.bin`],
    );
    assert.equal(await sandbox.files.read(`${root}/text.txt`), "hello\n");
    assert.deepEqual(
      Array.from(
        await sandbox.files.read(`${root}/binary.bin`, { format: "bytes" }),
      ),
      [0, 255, 16],
    );
    assert.equal(await sandbox.files.exists(`${relative}/cwd.txt`), true);

    const info = await sandbox.files.getInfo(`${root}/text.txt`);
    assert.equal(info.name, "text.txt");
    assert.equal(info.path, `${root}/text.txt`);
    assert.equal(info.type, FileType.FILE);
    assert.equal(info.size, 6);
    assert.equal(info.mode & 0o777, 0o644);
    assert.equal(info.permissions, "rw-r--r--");
    assert.equal(info.owner, "user");
    assert.equal(info.group, "user");
    assert.ok(info.modifiedTime instanceof Date);

    const entries = new Map(
      (await sandbox.files.list(root)).map((entry) => [entry.name, entry]),
    );
    assert.deepEqual(
      new Set(entries.keys()),
      new Set(["batch-a.txt", "batch-b.bin", "binary.bin", "text.txt"]),
    );
    assert.equal(entries.get("binary.bin")?.size, 3);
    assert.equal(entries.get("binary.bin")?.type, FileType.FILE);
    const renamed = await sandbox.files.rename(
      `${root}/text.txt`,
      `${root}/renamed.txt`,
    );
    assert.equal(renamed.path, `${root}/renamed.txt`);
    assert.equal(await sandbox.files.exists(`${root}/text.txt`), false);
    assert.equal(await sandbox.files.exists(`${root}/renamed.txt`), true);
    await sandbox.files.remove(`${root}/renamed.txt`);
    assert.equal(await sandbox.files.exists(`${root}/renamed.txt`), false);
  } finally {
    await sandbox.kill();
  }
}

if (!process.env.E2B_API_KEY || !process.env.ARKER_API_KEY)
  throw new Error("Live E2B and Arker credentials required");
const directory = await mkdtemp(join(tmpdir(), "compat-e2b-files-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
const nativeKey = process.env.E2B_API_KEY;
try {
  for (const [provider, constructor] of [
    ["e2b", Native],
    ["e2b", Shim],
    ["arker", Shim],
  ] as const) {
    const config = join(directory, "router.json");
    await writeFile(
      config,
      JSON.stringify({
        version: 1,
        existing: { strategy: "priority", order: [provider] },
        create: { strategy: "priority", order: [provider] },
      }),
    );
    process.env.COMPUTESDK_ROUTER_CONFIG = config;
    if (provider === "arker") delete process.env.E2B_API_KEY;
    else process.env.E2B_API_KEY = nativeKey;
    const called = new Set<string>();
    let outcome = "failed";
    try {
      await program(traceVendor(constructor, "Sandbox.static", called));
      outcome = "passed";
    } finally {
      await recordEvidence(
        "e2b",
        constructor === Native
          ? "reference"
          : provider === "arker"
            ? "arker"
            : "native",
        "import-only-filesystem-crud-metadata",
        [...called].sort(),
        outcome,
      );
    }
  }
} finally {
  if (nativeKey === undefined) delete process.env.E2B_API_KEY;
  else process.env.E2B_API_KEY = nativeKey;
  if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
  else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
  await rm(directory, { recursive: true });
}
