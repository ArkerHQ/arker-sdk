import assert from "node:assert/strict";
import { test } from "node:test";
import { DaytonaNotFoundError } from "@daytona/sdk";
import { ArkerError } from "../src/index.js";
import { Sandbox } from "../src/daytona.js";

function fixture(request?: (...args: any[]) => Promise<any>) {
  const calls: any[][] = [];
  const client = { controlBaseUrl: "https://control.invalid", _request: async (...args: any[]) => {
    calls.push(args);
    return request ? request(...args) : { vm_id: "source", state: args[1].endsWith("pause") ? "paused" : "running", mode: "full" };
  } };
  const vm: any = { id: "source", baseUrl: "https://region.invalid", _client: client };
  const box: any = { provider: "arker", sandboxId: vm.id, getInstance: () => vm };
  return { sandbox: new Sandbox(box), vm, client, calls };
}

test("pause preserves memory and resume infers mode without reading or patching policies", async () => {
  const { sandbox, calls } = fixture();
  await sandbox.pause(0);
  await sandbox.start();
  assert.deepEqual(calls.map((call) => call.slice(0, 3)), [
    ["POST", "/v1/vms/source/pause", { mode: "full" }],
    ["POST", "/v1/vms/source/resume", {}],
  ]);
  assert.equal(calls[0][6], false);
  assert.deepEqual(calls[0][7], {});
});

for (const method of ["stop", "archive"] as const) {
  test(`${method} refuses before mutation`, async () => {
    const { sandbox, calls } = fixture();
    await assert.rejects(sandbox[method](), (error: any) => error.code === "unsupported_operation");
    assert.equal(calls.length, 0);
  });
}

test("disk fork resumes child only, preserves source, and keeps accepted child on error", async () => {
  const { sandbox, vm, client, calls } = fixture(async () => { throw new ArkerError("not_found", "missing", 404); });
  vm.fork = async (options: any, transport: any) => {
    assert.deepEqual(options, { source_vm_id: "source", layers: ["disk"], durable: false });
    assert.deepEqual(transport, {});
    return { id: "child", baseUrl: vm.baseUrl, _client: client };
  };
  await assert.rejects(sandbox.fork(undefined, 0), (error: any) => error instanceof DaytonaNotFoundError && (error as DaytonaNotFoundError & { sandboxId: string }).sandboxId === "child");
  assert.deepEqual(calls[0].slice(0, 3), ["POST", "/v1/vms/child/resume", {}]);
  assert.equal(calls.length, 1);
});

for (const status of [401, 403, 404, 409]) {
  test(`named fork ${status} has no ordinary fallback or retry`, async () => {
    const { sandbox, calls } = fixture(async () => { throw new ArkerError("refused", "refused", status); });
    await assert.rejects(sandbox.fork({ name: "child" }));
    assert.equal(calls.length, 1);
    assert.equal(calls[0][1], "/v1/named-fork");
    assert.equal(calls[0][3], "https://control.invalid");
    assert.equal(calls[0][6], false);
  });
}

for (const [method, args] of [
  ["pause", [0]], ["start", [12]], ["stop", [3, true]], ["archive", []], ["fork", [{ name: "child" }, 7]],
] as const) {
  test(`native ${method} preserves result and error identity`, async () => {
    const result = {};
    const error = new DaytonaNotFoundError("missing");
    let fail = false;
    const native = { [method]: async (...actual: any[]) => { assert.deepEqual(actual, args); if (fail) throw error; return result; } };
    const sandbox = new Sandbox({ provider: "daytona", sandboxId: "native", getInstance: () => native } as any);
    assert.equal(await (sandbox[method] as Function)(...args), result);
    fail = true;
    await assert.rejects((sandbox[method] as Function)(...args), (caught) => caught === error);
  });
}

test("negative timeout does not dispatch", async () => {
  const { sandbox, calls } = fixture();
  await assert.rejects(sandbox.pause(-1));
  assert.equal(calls.length, 0);
});

test("named child resumes through global authority and remains independently usable", async () => {
  const { sandbox, calls } = fixture(async (_method, path) => path === "/v1/named-fork"
    ? { vm_id: "child", state: "idle", sessions: [], network: {}, resources: {} }
    : { vm_id: "child", state: "running" });
  const child = await sandbox.fork({ name: "child-name" }, 0);
  assert.equal(child.id, "child");
  assert.deepEqual(calls[0][2], { source_vm_id: "source", layers: ["disk"], durable: false, name: "child-name" });
  assert.deepEqual(calls[0][4], { "Arker-VM-Name": "child-name", "Arker-VM-Name-Mode": "create" });
  assert.equal(calls[1][1], "/v1/vms/child/resume");
  assert.equal(calls[1][3], "https://control.invalid");
});

test("response loss never falls through to ordinary creation", async () => {
  const lost = new Error("unknown outcome");
  const { sandbox, calls } = fixture(async () => { throw lost; });
  await assert.rejects(sandbox.fork({ name: "recoverable" }), (error) => error === lost);
  assert.equal(calls.length, 1);
});
