import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, chmodSync, statSync, readFileSync, writeFileSync, symlinkSync, existsSync, rmSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { join, dirname } from "node:path";
import { test } from "node:test";
import { Readable } from "node:stream";
import { finished } from "node:stream/promises";
import { DaytonaBadRequestError, DaytonaForbiddenError, DaytonaNotFoundError } from "@daytona/sdk";
import { Sandbox } from "../src/daytona.js";

function guest() {
  const home = mkdtempSync(join(tmpdir(), "daytona-filesystem-test-"));
  chmodSync(home, 0o777);
  const uid = process.getuid!() === 0 ? 65534 : process.getuid!();
  const gid = process.getuid!() === 0 ? 65534 : process.getgid!();
  const name = process.getuid!() === 0 ? "nobody" : userInfo().username;
  const replacement = `pwd.struct_passwd(${JSON.stringify([name, "x", uid, gid, "", home, "/bin/sh"])})`;
  const stages: string[] = [];
  const box = {
    provider: "arker", sandboxId: "local",
    runCommand: async (command: string) => {
      command = command.replace('setpriv --reuid=daytona --regid="$(id -g daytona)" --init-groups --', `setpriv --reuid=${uid} --regid=${gid} --keep-groups --`);
      try {
        const stdout = execFileSync("sh", ["-c", command.replace('pwd.getpwnam("daytona")', replacement)], {
          encoding: "utf8", timeout: 5000, stdio: ["ignore", "pipe", "pipe"],
        });
        return { stdout, stderr: "", exitCode: 0 };
      } catch (error: any) {
        assert.notEqual(error.code, "ETIMEDOUT", "filesystem calls must not block on FIFOs");
        return { stdout: "", stderr: String(error.stderr), exitCode: error.status ?? 1 };
      }
    },
    getInstance: () => ({
      async *_syncReadChunks(path: string) {
        stages.push(path);
        const bytes = readFileSync(path);
        for (let offset = 0; offset < bytes.length; offset += 65536) yield bytes.subarray(offset, offset + 65536);
      },
      async _syncWriteChunks(path: string, size: number, read: (offset: number, length: number) => Promise<Uint8Array>) {
        stages.push(path);
        const chunks: Uint8Array[] = [];
        for (let offset = 0; offset < size; offset += 65536) chunks.push(await read(offset, Math.min(65536, size - offset)));
        writeFileSync(path, Buffer.concat(chunks));
      },
      sync: async (path: string, data?: Uint8Array) => {
      stages.push(path);
      assert.equal(statSync(dirname(path)).mode & 0o777, 0o700);
      assert.ok(statSync(path).isFile());
      if (data !== undefined) { writeFileSync(path, data); return; }
      return readFileSync(path);
    } }),
  };
  const sandbox = Reflect.construct(Sandbox, [box]) as Sandbox;
  return { sandbox, home, uid, box, close: () => {
    for (const stage of stages) assert.ok(!existsSync(dirname(stage)), "staging must be cleaned");
    rmSync(home, { recursive: true, force: true });
  } };
}

test("Daytona buffered filesystem types, metadata, binary transfers and mutations", async () => {
  const g = guest();
  const fs = g.sandbox.fs;
  try {
    await fs.createFolder("dir/nested", "750");
    const bytes = Buffer.from(Array.from({ length: 256 }, (_, i) => i));
    await fs.uploadFile(bytes, "dir/nested/line\nbreak");
    assert.deepEqual(await fs.downloadFile("dir/nested/line\nbreak"), bytes);
    const info = await fs.getFileDetails("dir/nested/line\nbreak");
    assert.equal(info.owner, String(g.uid));
    assert.equal(info.size, 256);
    await fs.setFilePermissions("dir/nested/line\nbreak", { mode: "640" });
    await fs.uploadFile(Buffer.from("short"), "dir/nested/line\nbreak");
    assert.equal((await fs.getFileDetails("dir/nested/line\nbreak")).permissions, "0640");
    const local = join(g.home, "local-input");
    writeFileSync(local, bytes);
    await fs.uploadFiles([{ source: local, destination: "dir/local" }, { source: Buffer.alloc(0), destination: "dir/empty" }]);
    const output = join(g.home, "local-output");
    assert.equal(await fs.downloadFile("dir/local", output), undefined);
    assert.deepEqual(readFileSync(output), bytes);
    assert.deepEqual(await fs.downloadFile("dir/empty"), Buffer.alloc(0));
    assert.equal((await fs.listFiles("dir", { depth: 2 })).length, 4);
    await fs.moveFiles("dir/local", "dir/moved");
    await assert.rejects(fs.getFileDetails("dir/local"), DaytonaNotFoundError);
    await assert.rejects(fs.deleteFile("dir"), DaytonaBadRequestError);
    await fs.deleteFile("dir", true);
    assert.ok(!existsSync(join(g.home, "dir")));
  } finally { g.close(); }
});

test("Daytona guest identity prevents symlink privilege escalation and rejects FIFO transfers", async () => {
  const g = guest();
  const fs = g.sandbox.fs;
  try {
    const protectedFile = join(g.home, "protected");
    writeFileSync(protectedFile, "protected", { mode: 0o000 });
    symlinkSync(protectedFile, join(g.home, "link"));
    await assert.rejects(fs.downloadFile("link"), DaytonaForbiddenError);
    await assert.rejects(fs.uploadFile(Buffer.from("bad"), "link"), DaytonaForbiddenError);
    chmodSync(protectedFile, 0o400);
    assert.equal(readFileSync(protectedFile, "utf8"), "protected");
    await fs.uploadFile(Buffer.from("allowed"), "owned");
    symlinkSync(join(g.home, "owned"), join(g.home, "owned-link"));
    assert.equal((await fs.downloadFile("owned-link")).toString(), "allowed");
    if (process.getuid!() === 0) await assert.rejects(fs.setFilePermissions("owned", { owner: "0" }), DaytonaForbiddenError);
    const fifo = join(g.home, "fifo");
    execFileSync("mkfifo", [fifo]);
    chmodSync(fifo, 0o666);
    await assert.rejects(fs.downloadFile("fifo"), DaytonaBadRequestError);
    await assert.rejects(fs.uploadFile(Buffer.from("bad"), "fifo"), DaytonaBadRequestError);
    await assert.rejects(fs.downloadFile("owned", 0), /timeouts are not supported/);
    await assert.rejects(fs.listFiles(".", { depth: 0 }), /depth/);
    await assert.rejects(fs.downloadFileStream("owned", 0), /timeouts are not supported/);
  } finally { g.close(); }
});

test("Daytona bulk download preserves successes and typed per-file failures", async () => {
  const g = guest();
  try {
    await g.sandbox.fs.uploadFile(Buffer.from("ok"), "present");
    const destination = join(g.home, "downloaded");
    const results = await g.sandbox.fs.downloadFiles([{ source: "missing" }, { source: "present" }, { source: "present", destination }]);
    assert.equal(results[0]!.errorDetails?.statusCode, 404);
    assert.deepEqual(results[1]!.result, Buffer.from("ok"));
    assert.equal(results[2]!.result, destination);
    assert.equal(readFileSync(destination, "utf8"), "ok");
  } finally { g.close(); }
});

test("Daytona preserves native filesystem identity and refuses unowned staging cleanup", async () => {
  const native = { fs: { marker: "native" }, process: {} };
  const nativeSandbox = Reflect.construct(Sandbox, [{ provider: "daytona", sandboxId: "native", getInstance: () => native }]) as Sandbox;
  assert.equal(nativeSandbox.fs, native.fs);
  const commands: string[] = [];
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "local", getInstance: () => ({ sync: () => assert.fail("must not sync") }),
    runCommand: async (command: string) => { commands.push(command); return { stdout: "", stderr: "File exists", exitCode: 1 }; },
  }]) as Sandbox;
  await assert.rejects(sandbox.fs.uploadFile(Buffer.from("x"), "file"), /File exists/);
  assert.equal(commands.length, 1);
  assert.match(commands[0]!, /^mkdir -m 700 -- /);
});

test("Daytona interrupted upload preserves its primary error despite cleanup failure", async () => {
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "local", getInstance: () => ({ sync: async () => { throw new Error("upload interrupted"); } }),
    runCommand: async (command: string) => {
      if (command.startsWith("rm ")) throw new Error("cleanup failure");
      return { stdout: "", stderr: "", exitCode: 0 };
    },
  }]) as Sandbox;
  await assert.rejects(sandbox.fs.uploadFile(Buffer.from("x"), "file"), /upload interrupted/);
});

test("live scenario preserves primary failure while attempting deletion and disposal", async () => {
  const { program } = await import("./compat-daytona-filesystem-live.js");
  const primary = new Error("controlled primary failure");
  const actions: string[] = [];
  class Client {
    async create() { return { fs: { createFolder: async () => { throw primary; } } }; }
    async delete() { actions.push("delete"); throw new Error("controlled deletion failure"); }
    async [Symbol.asyncDispose]() { actions.push("dispose"); throw new Error("controlled disposal failure"); }
  }
  const stages: string[] = [];
  await assert.rejects(program(Client as any, stage => stages.push(stage)), error => error === primary);
  assert.equal(stages.at(-1), "create-folder");
  assert.deepEqual(actions, ["delete", "dispose"]);
});


test("Daytona commands share filesystem account, home and permissions", async () => {
  const g = guest();
  try {
    assert.equal(await g.sandbox.getUserHomeDir(), g.home);
    const result = await g.sandbox.process.executeCommand('id -u; printf "%s\\n" "$HOME"; pwd');
    const uid = process.getuid!() === 0 ? 65534 : process.getuid!();
    assert.equal(result.exitCode, 0);
    assert.deepEqual(result.result.trim().split("\n"), [String(uid), g.home, g.home]);
    await g.sandbox.fs.createFolder("commands", "700");
    assert.equal((await g.sandbox.process.executeCommand('printf "%s" "$VALUE" > from-command', "commands", { VALUE: "user-owned" })).exitCode, 0);
    assert.equal((await g.sandbox.fs.downloadFile("commands/from-command")).toString(), "user-owned");
    assert.equal((await g.sandbox.fs.getFileDetails("commands/from-command")).owner, String(uid));
    const protectedFile = join(g.home, "protected-command");
    writeFileSync(protectedFile, "unchanged", { mode: 0o000 });
    assert.notEqual((await g.sandbox.process.executeCommand("cat protected-command")).exitCode, 0);
    assert.notEqual((await g.sandbox.process.executeCommand("printf bad > protected-command")).exitCode, 0);
    chmodSync(protectedFile, 0o600);
    assert.equal(readFileSync(protectedFile, "utf8"), "unchanged");
    assert.equal((await g.sandbox.process.executeCommand("exit 17")).exitCode, 17);
    assert.equal((await g.sandbox.process.executeCommand("pwd")).result.trim(), g.home);
  } finally { g.close(); }
});

test("Daytona home lookup preserves native routing and rejects failed guest commands", async () => {
  const native = { fs: {}, process: {}, getUserHomeDir: async () => "/native-home" };
  const sandbox = Reflect.construct(Sandbox, [{ provider: "daytona", sandboxId: "native", getInstance: () => native }]) as Sandbox;
  assert.equal(await sandbox.getUserHomeDir(), "/native-home");
  const failed = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "local", getInstance: () => ({}),
    runCommand: async () => ({ stdout: "", stderr: "", exitCode: 1 }),
  }]) as Sandbox;
  await assert.rejects(failed.getUserHomeDir(), /Failed to get user home directory/);
});


test("Daytona streams preserve binary data, progress and unknown-length input", async () => {
  const g = guest();
  try {
    const bytes = Buffer.from(Array.from({ length: 200003 }, (_, i) => i % 256));
    const sent: number[] = [];
    await g.sandbox.fs.uploadFileStream(Readable.from([bytes.subarray(0, 17), bytes.subarray(17)]), "streamed", {
      onProgress: progress => sent.push(progress.bytesSent),
    });
    const received: number[] = [];
    const stream = await g.sandbox.fs.downloadFileStream("streamed", { onProgress: progress => received.push(progress.bytesReceived) });
    const chunks: Buffer[] = [];
    for await (const chunk of stream) chunks.push(chunk);
    assert.deepEqual(Buffer.concat(chunks), bytes);
    assert.equal(sent.at(-1), bytes.length);
    assert.equal(received.at(-1), bytes.length);
  } finally { g.close(); }
});

test("Daytona download destroys unconsumed streams and cleans staging", async () => {
  const g = guest();
  try {
    await g.sandbox.fs.uploadFile(Buffer.from("content"), "streamed");
    const stream = await g.sandbox.fs.downloadFileStream("streamed");
    const completion = finished(stream);
    stream.destroy();
    await assert.rejects(completion, { code: "ERR_STREAM_PREMATURE_CLOSE" });
    const controller = new AbortController();
    const cancelled = await g.sandbox.fs.downloadFileStream("streamed", { signal: controller.signal });
    const cancellation = finished(cancelled);
    controller.abort();
    await assert.rejects(cancellation, /Download cancelled/);
  } finally { g.close(); }
});
