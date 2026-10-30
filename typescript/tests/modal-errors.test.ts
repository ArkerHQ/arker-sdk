import assert from "node:assert/strict";
import { test } from "node:test";
import { ConflictError } from "modal";
import { createRequire } from "node:module";
const require = createRequire(typeof import.meta.url === "string" ? import.meta.url : __filename);
const { ClientError, Status } = createRequire(require.resolve("modal"))("nice-grpc");
import { ArkerError } from "../src/index.js";
import { Sandbox } from "../src/modal.js";
import { GuestProcesses } from "../src/compat/guest-process.js";

function missing(resource = "vm", status = 404) {
  return new ArkerError("not_found", "Unavailable", status,
    { code: "not_found", message: "Unavailable", details: { resource }, timestamp: "2026-01-01T00:00:00Z", request_id: "test", request: { kind: "matched", operation_id: "createRun" } } as never);
}
for (const deleted of [false, true]) {
  for (const staging of [false, true]) {
    test(`exec preserves native shutdown error identity (deleted=${deleted}, staging=${staging})`, async t => {
      const error = missing();
      const calls: string[] = [];
      const spawn = t.mock.method(GuestProcesses.prototype, "exec", async () => { calls.push("exec"); throw error; });
      const vm = { metadata: {}, refresh: async () => {
        calls.push("refresh"); if (deleted) throw missing(); return vm;
      } };
      const box = new Sandbox({ sandboxId: "owned", provider: "arker", getInstance: () => vm,
        runCommand: async () => { calls.push("guard"); throw error; },
      } as never);
      const expected = deleted ? ClientError : ConflictError;
      await assert.rejects(box.exec(["true"], staging ? { env: { VALUE: "synthetic" } } : undefined), e => {
        assert.ok(e instanceof Error);
        assert.ok(e instanceof expected);
        assert.equal(Object.getPrototypeOf(e), expected.prototype);
        if (deleted) {
          assert.equal((e as Error & { code: number }).code, Status.NOT_FOUND);
          assert.equal((e as Error & { path: string }).path, "/modal.task_command_router.TaskCommandRouter/TaskExecStart");
        }
        assert.ok(e.cause === error || (e.cause instanceof AggregateError && e.cause.errors[0] === error));
        return true;
      });
      assert.equal(calls.filter(x => x === "refresh").length, 1);
      assert.equal(spawn.mock.callCount(), staging ? 0 : 1);
    });
  }
}
for (const error of [missing("file"), missing("vm", 403), new Error("transport"),
  Object.assign(new Error("wrong brand"), { status: 404, code: "not_found", body: missing().body }),
  new ArkerError("not_found", "malformed body", 404, { code: "not_found", details: { resource: "vm" } } as never),
]) {
  test(`exec retains unrelated ${error.message}`, async t => {
    t.mock.method(GuestProcesses.prototype, "exec", async () => { throw error; });
    const box = new Sandbox({ sandboxId: "owned", provider: "arker", getInstance: () => ({ metadata: {},
      refresh: async () => assert.fail("unrelated errors must not trigger phase lookup"),
    }) } as never);
    await assert.rejects(box.exec(["true"]), e => e === error);
  });
}

test("failed phase lookup retains the primary error and all cleanup causes", async t => {
  const primary = missing();
  const error = new AggregateError([primary, new Error("cleanup denied")], "cleanup failed");
  t.mock.method(GuestProcesses.prototype, "exec", async () => { throw error; });
  const box = new Sandbox({ sandboxId: "owned", provider: "arker", getInstance: () => ({ metadata: {},
    refresh: async () => { throw new Error("lookup transport failure"); },
  }) } as never);
  await assert.rejects(box.exec(["true"]), e => e === error);
});

test("native route leaves errors untouched", async () => {
  const error = missing();
  const box = new Sandbox({ sandboxId: "native", provider: "modal", getInstance: () => ({
    exec: async () => { throw error; },
  }) } as never);
  await assert.rejects(box.exec(["true"]), e => e === error);
});
