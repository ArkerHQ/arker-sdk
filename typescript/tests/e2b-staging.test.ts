import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, statSync, lstatSync, symlinkSync, unlinkSync, rmSync, chmodSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { test } from "node:test";
import { Sandbox } from "../src/e2b.js";

for (const attack of ["symlink", "fifo"] as const) {
  test(`explicit-user reads never sync attacker-controlled ${attack} staging`, async () => {
    const directory = mkdtempSync(join(tmpdir(), "e2b-staging-test-"));
    const source = join(directory, "source");
    const protectedFile = join(directory, "protected");
    writeFileSync(source, "requested");
    writeFileSync(protectedFile, "protected");
    const stages: string[] = [];
    const sandbox = Reflect.construct(Sandbox, [{
      provider: "arker", sandboxId: "local",
      runCommand: async (command: string) => {
        command = command.replaceAll("--reuid=user --regid=user --init-groups", `--reuid=${process.getuid!()} --regid=${process.getgid!()} --keep-groups`);
        try {
          return { stdout: execFileSync("sh", ["-c", command], { encoding: "utf8", timeout: 3000 }), stderr: "", exitCode: 0 };
        } catch (error: any) { return { stdout: "", stderr: String(error.stderr), exitCode: error.status ?? 1 }; }
      },
      getInstance: () => ({ sync: async (path: string) => {
        stages.push(path);
        if (statSync(dirname(path)).mode & 0o022) {
          unlinkSync(path);
          if (attack === "symlink") symlinkSync(protectedFile, path);
          else execFileSync("mkfifo", [path]);
        }
        assert.ok(lstatSync(path).isFile(), "privileged sync must not open an attacker symlink/FIFO");
        return readFileSync(path);
      } }),
    }]) as Sandbox;
    try {
      assert.equal(await sandbox.files.read(source, { user: "user" }), "requested");
      assert.equal(readFileSync(protectedFile, "utf8"), "protected");
    } finally {
      for (const path of stages) rmSync(path, { force: true });
      rmSync(directory, { recursive: true, force: true });
    }
  });
}

test("requested-user reads and writes enforce permissions and reject FIFOs without blocking", async () => {
  const directory = mkdtempSync(join(tmpdir(), "e2b-permissions-test-"));
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
        assert.notEqual(error.code, "ETIMEDOUT", "file operations must not block on FIFOs");
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
    const destination = join(directory, "binary");
    const payload = Uint8Array.from({ length: 256 }, (_, index) => index);
    await sandbox.files.write(destination, payload.buffer);
    assert.equal(statSync(destination).uid, uid);
    assert.deepEqual(await sandbox.files.read(destination, { user: "user", format: "bytes" }), payload);
    chmodSync(destination, 0o640);
    await sandbox.files.write(destination, "short");
    assert.equal(statSync(destination).mode & 0o777, 0o640);
    assert.equal(readFileSync(destination, "utf8"), "short");
    const link = join(directory, "link");
    symlinkSync(destination, link);
    await sandbox.files.write(link, "linked");
    assert.equal(await sandbox.files.read(link, { user: "user" }), "linked");
    const protectedFile = join(directory, "protected");
    writeFileSync(protectedFile, "unchanged", { mode: 0o400 });
    unlinkSync(link);
    symlinkSync(protectedFile, link);
    await assert.rejects(sandbox.files.write(link, "forbidden"), /Permission denied/);
    chmodSync(protectedFile, 0o000);
    await assert.rejects(sandbox.files.read(link, { user: "user" }), /Permission denied/);
    chmodSync(protectedFile, 0o400);
    assert.equal(readFileSync(protectedFile, "utf8"), "unchanged");
    const fifo = join(directory, "fifo");
    execFileSync("mkfifo", [fifo]);
    chmodSync(fifo, 0o666);
    await assert.rejects(sandbox.files.read(fifo, { user: "user" }), /regular file/);
    await assert.rejects(sandbox.files.write(fifo, "must not block"));
    for (const stage of stages) assert.ok(!existsSync(dirname(stage)));
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test("read cleanup failure preserves the primary error", async () => {
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "local",
    getInstance: () => ({ sync: async () => { throw new Error("primary read failure"); } }),
    runCommand: async (command: string) => {
      if (command.startsWith("rm ")) throw new Error("cleanup failure");
      return { stdout: "", stderr: "", exitCode: 0 };
    },
  }]) as Sandbox;
  await assert.rejects(sandbox.files.read("/file", { user: "user" }), /primary read failure/);
});

test("staging reservation failure does not sync or delete someone else's path", async () => {
  for (const operation of ["read", "write"] as const) {
    const commands: string[] = [];
    const sandbox = Reflect.construct(Sandbox, [{
      provider: "arker", sandboxId: "local",
      getInstance: () => ({ sync: async () => assert.fail("must not sync") }),
      runCommand: async (command: string) => {
        commands.push(command);
        return { stdout: "", stderr: "File exists", exitCode: 1 };
      },
    }]) as Sandbox;
    await assert.rejects(operation === "read"
      ? sandbox.files.read("/file", { user: "user" })
      : sandbox.files.write("/file", "data"), /File exists/);
    assert.equal(commands.length, 1);
    assert.match(commands[0]!, /^mkdir -m 700 -- /);
  }
});
