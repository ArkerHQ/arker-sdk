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
    assert.deepEqual([...calls], ["Sandbox.trafficAccessToken", "Sandbox.sandboxId", "Sandbox.internal"]);
    assert.equal(sandbox.getInfo(), "info");
    assert.deepEqual([...calls], ["Sandbox.trafficAccessToken", "Sandbox.sandboxId", "Sandbox.internal", "Sandbox.getInfo"]);
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

for (const prefix of ["DriveInstance", "VolumeInstance", "Sandbox", "ContainerProcess"]) {
  test(`${prefix} credits only fields actually present and read`, async () => {
    const calls = new Set<string>();
    const value = { name: "fixture", optional: undefined, untouched: "no", _secret: "synthetic" };
    const traced = traceVendor(value, prefix, calls);
    assert.equal(traced.name, "fixture");
    assert.equal(traced.optional, undefined);
    Reflect.get(traced, "missing");
    Reflect.get(traced, "sandboxId");
    Reflect.get(traced, "then");
    void traced._secret;
    assert.equal(await Promise.resolve(traced), traced);
    assert.deepEqual([...calls], [`${prefix}.name`, `${prefix}.optional`]);
    assert.ok(!JSON.stringify([...calls]).includes("synthetic"));
  });
}

test("getter failure and unexecuted branches cannot credit properties or methods", () => {
  const calls = new Set<string>(), failure = new Error("getter failed");
  const traced = traceVendor({ get name() { throw failure; }, skipped() { throw failure; } }, "DriveInstance", calls);
  assert.throws(() => traced.name, error => error === failure);
  if (false) traced.skipped();
  void traced.skipped;
  assert.deepEqual([...calls], []);
});

test("repeated objects and methods retain identity and native receivers", async () => {
  const calls = new Set<string>();
  class Handle {
    #value = 7;
    get name() { return this.#value; }
    self() { return this; }
    getInfo() { return this.#value; }
  }
  const child = new Handle();
  const traced = traceVendor({ child }, "Sandbox", calls);
  assert.equal(traced.child, traced.child);
  assert.equal(traced.child.getInfo, traced.child.getInfo);
  assert.equal(traced.child.constructor, Handle);
  assert.equal(traced.child.getInfo(), 7);
  assert.equal(traced.child.self(), traced.child);
  const constructor = traceVendor({ create: async () => child }, "DriveInstance.static", calls);
  assert.equal(await constructor.create(), await constructor.create());
});

test("typed arrays and streams retain their original branded objects", async () => {
  const calls = new Set<string>();
  const bytes = new Uint8Array([1, 2, 3]);
  const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(bytes); controller.close(); } });
  const original = { bytes, stream };
  const traced = traceVendor(original, "ContainerProcess", calls);
  assert.equal(traced.bytes, bytes);
  assert.ok(ArrayBuffer.isView(traced.bytes));
  assert.deepEqual([...traced.bytes], [1, 2, 3]);
  assert.equal(traced.stream, stream);
  const reader = traced.stream.getReader();
  assert.equal((await reader.read()).value, bytes);
  reader.releaseLock();
  assert.deepEqual([...calls], ["ContainerProcess.bytes", "ContainerProcess.stream"]);
});

test("symbol iterators bind the actual receiver without crediting symbol calls", async () => {
  const calls = new Set<string>();
  class Values {
    #values = [1, 2];
    *[Symbol.iterator]() { yield* this.#values; }
    async *[Symbol.asyncIterator]() { yield* this.#values; }
  }
  const traced = traceVendor(new Values(), "Values", calls);
  assert.equal(traced[Symbol.iterator], traced[Symbol.iterator]);
  assert.deepEqual([...traced], [1, 2]);
  const received = [];
  for await (const value of traced) received.push(value);
  assert.deepEqual(received, [1, 2]);
  assert.deepEqual([...calls], []);
});

test("frozen own properties retain exact values required by Proxy invariants", () => {
  const child = { name: "fixture" }, method = () => child;
  const traced = traceVendor(Object.freeze({ child, method }), "DriveInstance", new Set());
  assert.equal(traced.child, child);
  assert.equal(traced.method, method);
});

test("thenable probes preserve resolution without API credit", async () => {
  const calls = new Set<string>();
  const value = { then(resolve: (value: number) => void) { assert.equal(this, value); resolve(9); } };
  assert.equal(await Promise.resolve(traceVendor(value, "Result", calls)), 9);
  assert.deepEqual([...calls], []);
});


test("ordinary method promises and returned data retain their native identity", async () => {
  const result = { name: "fixture" }, promise = Promise.resolve(result), calls = new Set<string>();
  const traced = traceVendor({ getInfo: () => promise }, "DriveInstance", calls);
  assert.equal(traced.getInfo(), promise);
  assert.equal(await traced.getInfo(), result);
  assert.deepEqual([...calls], ["DriveInstance.getInfo"]);
});


test("private nested data and absent factory results remain unchanged", () => {
  const calls = new Set<string>(), privateData = { token: "synthetic" };
  const traced = traceVendor({ _private: privateData, get: () => null }, "DriveInstance.static", calls);
  assert.equal(traced._private, privateData);
  assert.equal(traced._private.token, "synthetic");
  assert.equal(traced.get(), null);
  assert.deepEqual([...calls], ["DriveInstance.static.get"]);
});


test("immutable own methods with private state are left unwrapped", () => {
  class Handle {
    #id = 7;
    read: () => number;
    constructor() { this.read = function(this: Handle) { return this.#id; }; Object.freeze(this); }
  }
  const original = new Handle(), calls = new Set<string>();
  const traced = traceVendor(original, "CommandHandle", calls);
  assert.equal(traced, original);
  assert.equal(traced.read(), 7);
  assert.deepEqual([...calls], []);
});
