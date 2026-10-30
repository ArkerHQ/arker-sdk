import assert from "node:assert/strict";
import { test } from "node:test";
import { DaytonaNotFoundError } from "@daytona/sdk";
import { finished } from "node:stream/promises";
import * as streams from "../src/compat/daytona-streams.js";
const { downloadStream, uploadStream } = streams;

for (const operation of ["download-initial", "download-read", "download-destroy", "upload"] as const) {
  test(`Daytona ${operation} retains primary error and owned stage on cleanup failure`, async () => {
    const primary = new DaytonaNotFoundError("controlled missing file", 404);
    const cleanup = new Error("controlled cleanup failure");
    let stage = "";
    let removals = 0;
    const context = {
      command: () => "guest-copy",
      run: async (command: string) => {
        if (command.startsWith("mkdir")) stage = command.split(" -- ")[1]!;
        if (command.startsWith("rm")) { removals++; throw cleanup; }
        if (operation === "download-initial" && command.includes("guest-copy")) throw primary;
        return "";
      },
      vm: {
        async *_syncReadChunks() { if (operation === "download-read") throw primary; yield new Uint8Array([1]); },
        _syncWriteChunks: async () => { throw primary; },
      } as any,
    };
    let failure: any;
    try {
      if (operation === "upload") await uploadStream(context, Buffer.from("x"), "file", {});
      else {
        const stream = await downloadStream(context, "file", {});
        if (operation === "download-destroy") {
          const completion = finished(stream);
          stream.destroy(primary);
          await completion;
        } else for await (const _chunk of stream) { /* drain */ }
      }
    } catch (error) { failure = error; }
    assert.ok(failure instanceof AggregateError);
    function leaves(error: any): unknown[] { return error instanceof AggregateError ? error.errors.flatMap(leaves) : [error]; }
    assert.ok(leaves(failure).includes(primary));
    assert.ok(leaves(failure).includes(cleanup));
    assert.match(JSON.stringify((failure as AggregateError & { resources: string[] }).resources), /\.arker-daytona-fs-/);
    assert.ok(stage);
    assert.equal(removals, 1);
  });
}

test("Daytona cleanup attempts handle close and local removal independently", async () => {
  const primary = new DaytonaNotFoundError("native primary", 404);
  const stageError = new Error("stage failure");
  const handleError = new Error("handle failure");
  const removed: string[] = [];
  await assert.rejects(streams.cleanupTransfer([primary], [
    { path: "/owned/guest", close() { removed.push("stage"); throw stageError; } },
    { path: "/owned/local/data", close() { removed.push("handle"); throw handleError; } },
    { path: "/owned/local", close() { removed.push("local"); } },
  ]), (error: any) => {
    assert.deepEqual(error.errors, [primary, stageError, handleError]);
    assert.deepEqual(error.resources, ["/owned/guest", "/owned/local/data", "/owned/local"]);
    return true;
  });
  assert.deepEqual(removed, ["stage", "handle", "local"]);
  await assert.rejects(streams.cleanupTransfer([primary], [{ path: "/owned", close() {} }]), error => error === primary);
});

test("uncertain mkdir preserves error identity and reports path without deletion", async () => {
  const primary = new Error("lost mkdir response");
  const calls: string[] = [];
  const context = { command: () => "copy", vm: {} as any, run: async (command: string) => { calls.push(command); throw primary; } };
  await assert.rejects(downloadStream(context, "file", {}), (error: any) => {
    assert.equal(error, primary);
    assert.ok("stagingDirectory" in error);
    assert.match(String(error.stagingDirectory), /^\/tmp\/\.arker-daytona-fs-/);
    return true;
  });
  assert.equal(calls.length, 1);
});

for (const fails of [false, true]) test(`guest cleanup escapes the caller's transport scope, failure=${fails}`, async () => {
  const {currentRequestTransport, withRequestTransport} = await import("../src/internal/request-context.js");
  const controller = new AbortController();
  controller.abort(new Error("unrelated caller cancellation"));
  const outer = {signal: controller.signal, idleTimeoutMs: 1};
  const primary = new Error("upload failed");
  let cleanups = 0;
  const context = {
    command: () => "copy",
    run: async (command: string) => {
      if (command.startsWith("rm")) {
        cleanups++;
        assert.equal(currentRequestTransport(), undefined);
      }
      return "";
    },
    vm: {_syncWriteChunks: async () => {if (fails) throw primary;}} as any,
  };
  await withRequestTransport(outer, async () => {
    if (fails) await assert.rejects(uploadStream(context, Buffer.from("x"), "file", {}), error => error === primary);
    else await uploadStream(context, Buffer.from("x"), "file", {});
    assert.equal(currentRequestTransport(), outer);
  });
  assert.equal(cleanups, 1);
  assert.equal(currentRequestTransport(), undefined);
});
