import assert from "node:assert/strict";
import { test } from "node:test";
import type { SandboxInterface } from "computesdk";
import { createDaytonaTerminals } from "../src/compat/daytona-pty.js";
import { GuestProcesses, type GuestProcessResult } from "../src/compat/guest-process.js";

test("terminal output stays binary, callbacks are awaited, and nonzero exit is retained", async () => {
  const original = { exec: GuestProcesses.prototype.exec, read: GuestProcesses.prototype.readOutput };
  let request: Record<string, unknown> | undefined;
  let reads = 0;
  GuestProcesses.prototype.exec = async function (value) { request = value; return { name: "daytona-pty:unique" } as GuestProcessResult; };
  GuestProcesses.prototype.readOutput = async function () {
    reads++;
    return { data: Uint8Array.of(0, 255, 13, 10), nextOffset: 4, size: 4, eof: true, exitCode: 7 };
  };
  try {
    const box = { runCommand: async () => ({ exitCode: 0, stdout: '["/home/daytona","/bin/bash"]' }) } as unknown as SandboxInterface;
    const api = createDaytonaTerminals(box);
    const output: Uint8Array[] = [];
    const handle = await api.createPty({ id: "term", rows: 33, cols: 99, onData: async data => {
      await Promise.resolve(); output.push(data);
    } });
    assert.deepEqual(await handle.wait(), { exitCode: 7, error: "non-zero exit" });
    assert.deepEqual(output, [Uint8Array.of(0, 255, 13, 10)]);
    assert.deepEqual(request?.tty, { rows: 33, cols: 99 });
    assert.equal(request?.stdin, true);
    assert.equal(reads, 1);
    assert.equal(handle.isConnected(), false);
  } finally {
    GuestProcesses.prototype.exec = original.exec;
    GuestProcesses.prototype.readOutput = original.read;
  }
});

test("disconnect joins an outstanding read without killing the remote terminal", async () => {
  const original = { list: GuestProcesses.prototype.list, read: GuestProcesses.prototype.readOutput, kill: GuestProcesses.prototype.kill };
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  GuestProcesses.prototype.list = async () => [{ name: "daytona-pty:term", status: "running", command: '{"id":"term","cwd":"/tmp","envs":{}}', tty: { rows: 24, cols: 80 } } as GuestProcessResult];
  let calls = 0;
  GuestProcesses.prototype.readOutput = async (_id, _stream, offset) => {
    if (calls++ === 0) return { data: Buffer.from("old-output"), nextOffset: 10, size: 10, eof: false, exitCode: null };
    assert.equal(offset, 10);
    await pending;
    return { data: new Uint8Array(), nextOffset: 10, size: 10, eof: false, exitCode: null };
  };
  GuestProcesses.prototype.kill = async () => { throw new Error("Disconnect must not kill"); };
  try {
    const handle = await createDaytonaTerminals({} as SandboxInterface).connectPty("term");
    let completed = false;
    const disconnected = handle.disconnect().then(() => { completed = true; });
    await Promise.resolve();
    assert.equal(completed, false);
    release(); await disconnected;
    assert.equal(handle.isConnected(), false);
    assert.deepEqual(await handle.wait(), { exitCode: undefined, error: undefined });
  } finally {
    GuestProcesses.prototype.list = original.list;
    GuestProcesses.prototype.readOutput = original.read;
    GuestProcesses.prototype.kill = original.kill;
  }
});

test("a terminal callback can await disconnect without awaiting itself", async () => {
  const original = { list: GuestProcesses.prototype.list, read: GuestProcesses.prototype.readOutput };
  GuestProcesses.prototype.list = async () => [{ name: "daytona-pty:term", status: "running", command: '{"id":"term"}' } as GuestProcessResult];
  let reads = 0;
  GuestProcesses.prototype.readOutput = async () => {
    await new Promise(resolve => setTimeout(resolve, 0));
    return { data: Buffer.from(reads++ ? "new" : ""), nextOffset: 3, size: 3, eof: false, exitCode: null };
  };
  try {
    let completed = false;
    const handle = await createDaytonaTerminals({} as SandboxInterface).connectPty("term", { onData: async () => {
      await handle.disconnect(); completed = true;
    } });
    await Promise.race([handle.wait(), new Promise((_, reject) => setTimeout(() => reject(new Error("reentrant disconnect blocked")), 1000))]);
    assert.equal(completed, true);
    assert.equal(handle.error, undefined);
  } finally {
    GuestProcesses.prototype.list = original.list;
    GuestProcesses.prototype.readOutput = original.read;
  }
});

for (const terminationSignal of [null, "SIGKILL"]) {
  test(`exit137 preserves actual signal metadata: ${terminationSignal}`, async () => {
    const original = { exec: GuestProcesses.prototype.exec, read: GuestProcesses.prototype.readOutput };
    GuestProcesses.prototype.exec = async () => ({ name: "owned" } as GuestProcessResult);
    GuestProcesses.prototype.readOutput = async () => ({ data: new Uint8Array(), nextOffset: 0, size: 0,
      eof: true, exitCode: 137, terminationSignal });
    try {
      const box = { runCommand: async () => ({ exitCode: 0, stdout: '["/home/daytona","/bin/bash"]' }) } as unknown as SandboxInterface;
      const handle = await createDaytonaTerminals(box).createPty({ id: "term", onData: () => {} });
      assert.deepEqual(await handle.wait(), { exitCode: 137, error: terminationSignal ?? "non-zero exit" });
      await handle.disconnect();
    } finally {
      GuestProcesses.prototype.exec = original.exec;
      GuestProcesses.prototype.readOutput = original.read;
    }
  });
}
