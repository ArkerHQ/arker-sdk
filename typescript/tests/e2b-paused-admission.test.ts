import assert from "node:assert/strict";
import { test } from "node:test";
import { SandboxNotRunningError } from "e2b";
import { Sandbox } from "../src/e2b.js";
import { ArkerError } from "../src/index.js";
import { e2bAdmissionError } from "../src/compat/e2b-errors.js";

function refusal(status = 409, code = "invalid_state", requirement = "explicit_resume", kind = "vm") {
  const body = { code, details: { resource: { kind, id: "vm-paused" }, requirement } };
  return new ArkerError(code, "explicit resume required", status, body as unknown as NonNullable<ArkerError["body"]>);
}

for (const operation of ["command", "background", "read", "bytes", "stream", "write", "pty"] as const) {
  test(`${operation} preserves data-plane pause refusal without probing or retrying`, async () => {
    const error = new SandboxNotRunningError("explicitly paused");
    let calls = 0;
    const reject = async () => { calls++; throw error; };
    const vm = { id: "vm-paused", sync: reject, run: reject, createSession: reject };
    const box = { provider: "arker", sandboxId: vm.id, getInstance: () => vm,
      runCommand: reject, filesystem: { readFile: reject, writeFile: reject } };
    const plane = { commands: { run: reject }, files: { read: reject, write: reject }, pty: { create: reject }, git: {}, close() {} };
    const sandbox = Reflect.construct(Sandbox, [box, undefined, undefined, plane]) as Sandbox;
    await assert.rejects(async () => {
      if (operation === "command" || operation === "background") {
        await sandbox.commands.run("printf must-not-run", { background: operation === "background" });
      } else if (operation === "pty") {
        await sandbox.pty.create({ rows: 24, cols: 80, onData() {} });
      } else if (operation === "write") {
        await sandbox.files.write("/tmp/value", "must-not-write");
      } else if (operation === "bytes") {
        await sandbox.files.read("/tmp/value", { format: "bytes" });
      } else if (operation === "stream") {
        await sandbox.files.read("/tmp/value", { format: "stream" });
      } else {
        await sandbox.files.read("/tmp/value");
      }
    }, SandboxNotRunningError);
    assert.equal(calls, 1);
  });
}

test("only explicit_resume VM refusals map; other conflicts and absence keep identity", () => {
  for (const error of [
    refusal(404), refusal(503), refusal(409, "conflict"),
    refusal(409, "invalid_state", "running"), refusal(409, "invalid_state", "mutable"),
    refusal(409, "invalid_state", "explicit_resume", "session"),
    new ArkerError("invalid_state", "explicit resume required", 409),
    new SandboxNotRunningError("native marker"),
  ]) assert.equal(e2bAdmissionError(error), error);
});

test("native command errors keep identity even if they resemble Arker refusals", async () => {
  const error = refusal();
  const native = { commands: { run: async () => { throw error; } }, files: {}, git: {} };
  const sandbox = Reflect.construct(Sandbox, [{ provider: "e2b", sandboxId: "native" }, native]) as Sandbox;
  await assert.rejects(sandbox.commands.run("true"), actual => actual === error);
});
