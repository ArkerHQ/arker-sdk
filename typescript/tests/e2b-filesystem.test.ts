import assert from "node:assert/strict";
import { test } from "node:test";
import { Sandbox } from "../src/e2b.js";

for (const native of [false, true]) {
  test(`${native ? "native" : "Arker"} facade preserves component identity, options and streams`, async () => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(Uint8Array.of(0, 255)); controller.close(); } });
    const options = { format: "stream" as const, user: "custom", requestTimeoutMs: 123 };
    const marker = { path: "/path", size: 2 };
    const files = {
      read(path: string, received: unknown) { assert.equal(this, files); assert.equal(path, "/path"); assert.equal(received, options); return Promise.resolve(stream); },
      write(path: string, data: unknown, received: unknown) { assert.equal(this, files); assert.equal(path, "/path"); assert.equal(data, stream); assert.equal(received, options); return Promise.resolve(marker); },
    };
    const components = { files, commands: {}, pty: {}, git: {}, close() {} };
    const forbidden = () => { throw new Error("Facade must not stage or translate files"); };
    const box = { sandboxId: "sandbox", provider: native ? "e2b" : "arker", runCommand: forbidden, getInstance: forbidden };
    const sandbox = Reflect.construct(Sandbox, [box, native ? components : undefined, undefined, native ? undefined : components]) as Sandbox;
    for (const key of ["commands", "files", "pty", "git"] as const) assert.equal(sandbox[key], components[key]);
    assert.equal(await sandbox.files.read("/path", options), stream);
    assert.equal(await sandbox.files.write("/path", stream, options), marker);
  });
}

test("facade leaves native overwrite errors unchanged", async () => {
  const error = new Error("500: error opening file: permission denied");
  const files = { write: async () => { throw error; } };
  const sandbox = Reflect.construct(Sandbox, [{ provider: "arker", sandboxId: "sandbox" }, undefined, undefined,
    { files, commands: {}, pty: {}, git: {}, close() {} }]) as Sandbox;
  await assert.rejects(sandbox.files.write("/tmp/existing", "replacement"), actual => actual === error);
});

test("commands and PTY preserve native handle, callbacks, options and error identity", async () => {
  const { CommandExitError } = await import("e2b");
  const failure = new CommandExitError({ stdout: "output", stderr: "failure", exitCode: 7 });
  const options = { user: "root", stdin: true, background: true as const, timeoutMs: 0, onStdout() {} };
  const handle = { pid: 123, wait: async () => { throw failure; } };
  const commands = { run(_command: string, received: unknown) { assert.equal(this, commands); assert.equal(received, options); return Promise.resolve(handle); } };
  const pty = { connect(pid: number) { assert.equal(this, pty); assert.equal(pid, handle.pid); return Promise.resolve(handle); } };
  const sandbox = Reflect.construct(Sandbox, [{ provider: "arker", sandboxId: "sandbox" }, undefined, undefined,
    { commands, pty, files: {}, git: {}, close() {} }]) as Sandbox;
  assert.equal(await sandbox.commands.run("exit 7", options), handle);
  assert.equal(await sandbox.pty.connect(handle.pid), handle);
  await assert.rejects(handle.wait(), error => error === failure && error instanceof CommandExitError);
});
