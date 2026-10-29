import assert from "node:assert/strict";
import test from "node:test";
import { traceVendor } from "./compat-trace.js";

test("sandbox properties record only actual reads and never credential values", () => {
  class Sandbox {
    #token: string | undefined;
    constructor(token?: string) { this.#token = token; }
    get trafficAccessToken() { return this.#token; }
    sandboxId = "fixture";
    sandboxDomain = "example.invalid";
    internal = "not-public";
    getInfo() { assert.ok(this instanceof Sandbox); return "info"; }
  }
  for (const token of [undefined, "synthetic-secret"]) {
    const calls = new Set<string>();
    const sandbox = traceVendor(new Sandbox(token), "Sandbox", calls);
    assert.equal(calls.size, 0);
    assert.equal(sandbox.trafficAccessToken, token);
    assert.equal(sandbox.sandboxId, "fixture");
    void sandbox.internal;
    void sandbox.getInfo;
    Reflect.get(sandbox, Symbol.toStringTag);
    Reflect.get(sandbox, "then");
    assert.deepEqual([...calls], ["Sandbox.trafficAccessToken", "Sandbox.sandboxId"]);
    assert.equal(sandbox.getInfo(), "info");
    assert.deepEqual([...calls], ["Sandbox.trafficAccessToken", "Sandbox.sandboxId", "Sandbox.getInfo"]);
    assert.ok(!JSON.stringify([...calls]).includes("synthetic-secret"));
    assert.ok(!calls.has("Sandbox.sandboxDomain"));
  }
});

for (const method of ["create", "connect"] as const) test(`PTY ${method} traces returned native CommandHandle APIs`, async () => {
  const calls = new Set<string>();
  const result = { stdout: "done", stderr: "", exitCode: 0 };
  class Handle {
    #pid = 123;
    get pid() { return this.#pid; }
    async wait() { assert.equal(this.#pid, 123); return result; }
    disconnect() { assert.equal(this.#pid, 123); }
  }
  const handle = new Handle();
  const pty = {
    async create() { assert.equal(this, pty); return handle; },
    async connect() { assert.equal(this, pty); return handle; },
  };
  const sandbox = traceVendor({ pty }, "Sandbox", calls);
  const observed = await sandbox.pty[method]();
  assert.deepEqual([...calls], ["Sandbox.pty", `Sandbox.pty.${method}`]);
  assert.equal(observed.pid, 123);
  assert.equal(await observed.wait(), result);
  observed.disconnect();
  assert.deepEqual([...calls], ["Sandbox.pty", `Sandbox.pty.${method}`, "CommandHandle.pid", "CommandHandle.wait", "CommandHandle.disconnect"]);
  assert.ok(!calls.has("CommandHandle.kill"));
});

test("foreground command results and failed calls do not invent handle observations", async () => {
  const calls = new Set<string>();
  const result = { stdout: "done", exitCode: 0 };
  const failure = new Error("failed");
  const sandbox = traceVendor({ commands: { run: async () => result, connect: async () => { throw failure; } } }, "Sandbox", calls);
  assert.equal(await sandbox.commands.run(), result);
  await assert.rejects(sandbox.commands.connect(), error => error === failure);
  assert.deepEqual([...calls], ["Sandbox.commands", "Sandbox.commands.run", "Sandbox.commands.connect"]);
});

for (const prefix of ["Sandbox", "Sandbox.static"]) test(`${prefix}.fork traces successful children without wrapping errors`, async () => {
  const calls = new Set<string>();
  class Child {
    #id = "child";
    getInfo() { return this.#id; }
    async kill() { assert.equal(this.#id, "child"); return true; }
  }
  const child = new Child(), failure = new Error("child failed");
  const parent = { async fork() { assert.equal(this, parent); return [child, failure]; } };
  const observed = traceVendor(parent, prefix, calls);
  const results = await observed.fork();
  assert.equal(results[1], failure);
  assert.ok(results[0] instanceof Child);
  assert.deepEqual([...calls], [`${prefix}.fork`]);
  const result = results[0] as Child;
  assert.equal(result.getInfo(), "child");
  assert.equal(await result.kill(), true);
  assert.deepEqual([...calls], [`${prefix}.fork`, "Sandbox.getInfo", "Sandbox.kill"]);
  assert.ok(!calls.has("Sandbox.files.read"));
});
