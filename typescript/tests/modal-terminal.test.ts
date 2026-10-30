import assert from "node:assert/strict";
import { test } from "node:test";
import { NotFoundError } from "modal";
import { Arker, ArkerError, VM, type RequestTransportOptions } from "../src/index.js";
import { Sandbox } from "../src/modal.js";
import { lifecycle, lookup } from "../src/compat/modal-lifecycle.js";

const reply = (reason: string | null = null) => ({ vm_id: "01ARZ3NDEKTSV4RRFFQ69G5FAV", state: reason ? "terminated" : "running", termination_reason: reason, terminated_at: reason ? "2026-01-01T00:00:00Z" : null });
function missing() {
  return new ArkerError("not_found", "Missing", 404, {code: "not_found", message: "Missing", details: {resource: "vm"}, timestamp: "2026-01-01T00:00:00Z", request_id: "test", request: {kind: "matched", operation_id: "getVmLifecycle"}});
}
function fixture(responses: unknown[]) {
  const calls: unknown[][] = [];
  const client = new Arker({ apiKey: "test-only", baseUrl: "https://fixture.invalid" });
  client._request = async (...args: unknown[]) => {
    calls.push(args);
    const value = responses.shift();
    if (value instanceof Error) throw value;
    return value as never;
  };
  const vm = new VM(client, "01ARZ3NDEKTSV4RRFFQ69G5FAV");
  const box = new Sandbox({ sandboxId: vm.id, provider: "arker", getInstance: () => vm } as never);
  return { vm, box, calls };
}
for (const [reason, code] of [["deadline", 124], ["requested", 137]] as const) {
  test(`native TS wait returns the recorded ${reason} code`, async () => {
    const {box, calls} = fixture([reply(reason), reply(reason)]);
    assert.equal(await box.poll(), code);
    assert.equal(await box.wait(), code);
    assert.equal(calls.length, 2);
    assert.equal(calls[0]![1], "/v1/vms/01ARZ3NDEKTSV4RRFFQ69G5FAV/lifecycle?include_terminated=true");
  });
}
for(const label of ["foreign", "nonexistent", "purged"]) test(`retained ${label} remains native NotFoundError`, async () => {
  const {vm, calls} = fixture([missing()]);
  await assert.rejects(lifecycle(vm), error => error instanceof NotFoundError && error.constructor === NotFoundError);
  assert.equal(calls.length, 1);
});
for(const response of [reply("unknown"), {...reply("requested"), vm_id: "foreign"}, {}]) test("unavailable results are not invented", async () => {
  const {box} = fixture([response]);
  await assert.rejects(box.poll());
});
test("lookup constructs a terminal VM only after server proof", async t => {
  const {vm, calls} = fixture([reply("requested")]);
  const transport = { signal: new AbortController().signal };
  t.mock.method(vm._client, "getVm", async (_id: string, _base: string | undefined, options?: RequestTransportOptions) => {
    assert.equal(options, transport);
    throw missing();
  });
  t.mock.method(vm._client, "vm", () => vm);
  assert.equal(await lookup(vm._client, vm.id, transport), vm);
  assert.equal(calls.length, 1);
  assert.equal(calls[0]![7], transport);
});
test("transport errors do not become terminal results", async () => {
  const error = new Error("transport");
  const {box} = fixture([error]);
  await assert.rejects(box.poll(), value => value === error);
});
for (const repeated of [false, true]) test(`terminate waits for recorded outcome (repeated=${repeated})`, async () => {
  const {box, calls} = fixture(repeated
    ? [missing(), reply("requested"), reply("requested")]
    : [{deleted: true}, reply("requested")]);
  assert.equal(await box.terminate({wait: true}), 137);
  assert.equal(calls[0]![0], "DELETE");
  assert.ok(calls.slice(1).every(call => call[0] === "GET"));
  assert.equal(calls.length, repeated ? 3 : 2);
});
