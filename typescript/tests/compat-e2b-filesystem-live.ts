import assert from "node:assert/strict";
import { FileType, Sandbox as Native } from "e2b";
import { Sandbox as Shim } from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { traceVendor } from "./compat-trace.js";
import { withLiveRouter } from "./compat-live-router.js";

type SandboxConstructor = { create(): Promise<Native | Shim> };

async function program(Sandbox: SandboxConstructor): Promise<void> {
  const sandbox = await Sandbox.create();
  const root = "/tmp/compat-e2b-files";
  const relative = "compat-e2b-relative";
  try {
    const identityCommand = `printf '%s|%s|%s|%s|%s|%s|%s' "$(id -un)" "$(id -u)" "$(id -g)" "$HOME" "$USER" "$LOGNAME" "$PWD"`;
    assert.equal((await sandbox.commands.run(identityCommand)).stdout, "user|1000|1000|/home/user|user|user|/home/user");
    assert.equal((await sandbox.commands.run(identityCommand, { user: "root" })).stdout, "root|0|0|/root|root|root|/home/user");
    assert.equal((await sandbox.commands.run(`printf %s "$COMPAT_ONCE"`, { envs: { COMPAT_ONCE: "one" } })).stdout, "one");
    assert.equal((await sandbox.commands.run(`printf %s "\${COMPAT_ONCE-unset}"`)).stdout, "unset");
    assert.equal((await sandbox.commands.run("pwd", { cwd: "/tmp" })).stdout.trim(), "/tmp");
    assert.equal((await sandbox.commands.run("pwd")).stdout.trim(), "/home/user");
    await sandbox.commands.run("umask 077");
    await sandbox.commands.run("printf secret > /tmp/compat-e2b-root-only && chmod 600 /tmp/compat-e2b-root-only", { user: "root" });
    assert.equal(await sandbox.files.read("/tmp/compat-e2b-root-only"), "secret");
    await sandbox.commands.run("rm -f /tmp/compat-e2b-root-only", { user: "root" });
    await sandbox.commands.run(`rm -rf ${root} /home/user/${relative}`);
    assert.equal(await sandbox.files.makeDir(root), true);
    assert.equal(await sandbox.files.makeDir(root), false);
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
    assert.equal(info.permissions, "-rw-r--r--");
    assert.equal(info.owner, "user");
    assert.equal(info.group, "user");
    assert.ok(info.modifiedTime instanceof Date);

    const formats = `${root}/formats`;
    const payload = Uint8Array.of(0, 255, 128, 10);
    const blob = new Blob([payload]);
    const uploaded = await sandbox.files.write([
      { path: `${formats}/blob`, data: blob },
      { path: `${formats}/stream`, data: blob.stream() },
    ], { user: "user" });
    assert.equal(uploaded.length, 2);
    assert.deepEqual(await sandbox.files.write([]), []);
    for (const entry of uploaded) {
      const readBlob = await sandbox.files.read(entry.path, { format: "blob", user: "user" });
      assert.deepEqual(new Uint8Array(await readBlob.arrayBuffer()), payload);
      const stream = await sandbox.files.read(entry.path, { format: "stream", user: "user" });
      assert.deepEqual(new Uint8Array(await new Response(stream).arrayBuffer()), payload);
    }
    assert.equal((await sandbox.files.list(root, { depth: 2 })).filter((entry) => entry.path.startsWith(`${formats}/`)).length, 2);
    await sandbox.files.remove(formats);

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

await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY"], async ({ setProvider }) => {
  const nativeKey = process.env.E2B_API_KEY;
  for (const [provider, constructor] of [
    ["e2b", Native],
    ["e2b", Shim],
    ["arker", Shim],
  ] as const) {
    await setProvider(provider);
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
});
