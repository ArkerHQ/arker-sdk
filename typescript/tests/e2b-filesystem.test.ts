import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, statSync, symlinkSync, rmSync, chmodSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { test } from "node:test";
import { FileType, InvalidArgumentError } from "e2b";
import { Sandbox } from "../src/e2b.js";

test("file formats, batch overload, recursive listing and mkdir use real guest commands", async () => {
  const directory = mkdtempSync(join(tmpdir(), "e2b-files-test-"));
  chmodSync(directory, 0o777);
  const uid = process.getuid!() === 0 ? 65534 : process.getuid!();
  const gid = process.getuid!() === 0 ? 65534 : process.getgid!();
  const stages: string[] = [];
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "local",
    runCommand: async (command: string) => {
      command = command.replaceAll("--reuid=user --regid=user --init-groups", `--reuid=${uid} --regid=${gid} --keep-groups`);
      try {
        return { stdout: execFileSync("sh", ["-c", command], { encoding: "utf8", timeout: 3000, stdio: ["ignore", "pipe", "pipe"] }), stderr: "", exitCode: 0 };
      } catch (error: any) {
        assert.notEqual(error.code, "ETIMEDOUT");
        return { stdout: "", stderr: String(error.stderr), exitCode: error.status ?? 1 };
      }
    },
    getInstance: () => ({ sync: async (path: string, data?: Uint8Array) => {
      stages.push(path);
      assert.equal(statSync(dirname(path)).mode & 0o777, 0o700);
      assert.equal(statSync(path).mode & 0o777, 0o600);
      if (data !== undefined) { writeFileSync(path, data); return; }
      return readFileSync(path);
    } }),
  }]) as Sandbox;
  try {
    const root = join(directory, "nested");
    assert.equal(await sandbox.files.makeDir(root), true);
    assert.equal(await sandbox.files.makeDir(root), false);
    const bytes = Uint8Array.of(0, 255, 128, 10);
    const blob = new Blob([bytes]);
    const paths = [join(root, "blob"), join(root, "stream")];
    const written = await sandbox.files.write([
      { path: paths[0]!, data: blob }, { path: paths[1]!, data: blob.stream() },
    ], { user: "user" });
    assert.deepEqual(written.map((file) => file.path), paths);
    assert.deepEqual(await sandbox.files.write([]), []);
    for (const path of paths) {
      assert.equal(statSync(path).uid, uid);
      assert.deepEqual(new Uint8Array(await (await sandbox.files.read(path, { user: "user", format: "blob" })).arrayBuffer()), bytes);
      assert.deepEqual(new Uint8Array(await new Response(await sandbox.files.read(path, { user: "user", format: "stream" })).arrayBuffer()), bytes);
    }
    await sandbox.files.write(join(root, "sub", "tab\tnewline\n"), "nested");
    symlinkSync("missing", join(root, "broken"));
    const shallow = await sandbox.files.list(root, { user: "user" });
    assert.equal(shallow.length, 4);
    assert.equal(shallow.find((entry) => entry.name === "broken")?.type, FileType.SYMLINK);
    const deep = await sandbox.files.list(root, { user: "user", depth: 2 });
    assert.equal(deep.length, 5);
    assert.ok(deep.some((entry) => entry.name === "tab\tnewline\n"));
    await assert.rejects(sandbox.files.list(root, { depth: 0 }), InvalidArgumentError);
    const failingStream = new ReadableStream({ start(controller) { controller.error(new Error("source failed")); } });
    await assert.rejects(sandbox.files.write(join(root, "failed"), failingStream), /source failed/);
    assert.equal(existsSync(join(root, "failed")), false);
    for (const path of stages) assert.equal(existsSync(dirname(path)), false);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
