import assert from "node:assert/strict";
import { test } from "node:test";
import { CommandExitError, TimeoutError } from "e2b";
import { e2bCommandHandle } from "../src/compat/e2b-command.js";
import type { GuestProcessResult } from "../src/compat/guest-process.js";

function fixture() {
  let current: GuestProcessResult = {
    name: "job",
    pid: "42",
    command: "cat",
    workingDir: "/home/user",
    status: "running",
    exitCode: 0,
    stdout: "",
    stderr: "",
    logs: "",
    startedAt: "now",
    completedAt: "",
    stdin: true,
  };
  const inputs: Array<string | Uint8Array> = [];
  let kills = 0;
  const manager = {
    get: async () => current,
    kill: async () => {
      kills++;
      current = { ...current, status: "killed", exitCode: -9 };
      return { message: "killed" };
    },
    writeStdin: async (_id: string, data: string | Uint8Array) => {
      inputs.push(data);
      return { message: "written" };
    },
    closeStdin: async () => {
      current = {
        ...current,
        status: "failed",
        stdout: "hello\n",
        stderr: "diagnostic",
        exitCode: 17,
      };
      return { message: "closed" };
    },
  };
  return { manager, inputs, started: current, kills: () => kills };
}

test(
  "E2B handles preserve public members, stdin and typed wait errors",
  { timeout: 2000 },
  async () => {
    const value = fixture();
    const output: string[] = [];
    const handle = e2bCommandHandle(value.manager, value.started, {
      onStdout: (chunk) => {
        output.push(chunk);
      },
    });
    assert.equal(handle.pid, 42);
    await handle.sendStdin("hello\n");
    await handle.closeStdin();
    await assert.rejects(handle.wait(), (error: unknown) => {
      assert.ok(error instanceof CommandExitError);
      assert.deepEqual(
        [error.stdout, error.stderr, error.exitCode, error.error],
        ["hello\n", "diagnostic", 17, "exit status 17"],
      );
      return true;
    });
    assert.deepEqual(value.inputs, ["hello\n"]);
    assert.equal(output.join(""), "hello\n");
  },
);

test(
  "E2B kill preserves signed signal semantics without changing Blaxel",
  { timeout: 2000 },
  async () => {
    const value = fixture();
    const handle = e2bCommandHandle(value.manager, value.started);
    assert.equal(await handle.kill(), true);
    await assert.rejects(handle.wait(), (error: unknown) => {
      assert.ok(error instanceof CommandExitError);
      assert.deepEqual([error.exitCode, error.error], [-1, "signal: killed"]);
      return true;
    });
    assert.equal(await handle.kill(), false);
    assert.equal(value.kills(), 1);
  },
);

test(
  "E2B observation timeout never kills the process",
  { timeout: 2000 },
  async () => {
    const value = fixture();
    const handle = e2bCommandHandle(value.manager, value.started, {
      timeoutMs: 1,
    });
    await assert.rejects(handle.wait(), TimeoutError);
    assert.equal(value.kills(), 0);
  },
);
