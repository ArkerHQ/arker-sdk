import assert from "node:assert/strict";
import { test } from "node:test";
import { RequestIdleTimeoutError } from "../src/internal/idle-fetch.js";
import { downloadStream, uploadStream } from "../src/compat/daytona-streams.js";
import { DaytonaConnectionTimeoutError } from "@daytona/sdk";
for (const operation of ["upload", "download"] as const) test(`${operation} timeout cleanup has no transport deadline`, async () => {
  const calls: Array<{command: string; transport: unknown}> = [];
  const context = {
    command: () => "guest-copy",
    run: async (command: string, transport?: unknown) => { calls.push({command, transport}); return ""; },
    vm: {
      async *_syncReadChunks() { throw new RequestIdleTimeoutError(10); },
      async _syncWriteChunks() { throw new RequestIdleTimeoutError(10); },
    } as any,
  };
  await assert.rejects(async () => {
    if (operation === "upload") await uploadStream(context, Buffer.from("x"), "file", {timeout: .01});
    else for await (const _ of await downloadStream(context, "file", {timeout: .01})) { /* drain */ }
  }, DaytonaConnectionTimeoutError);
  assert.deepEqual(calls[0]!.transport, {signal: undefined, idleTimeoutMs: 10});
  assert.ok(calls.at(-1)!.command.startsWith("rm "));
  assert.equal(calls.at(-1)!.transport, undefined);
});


test("upload idle timeout destroys a stalled source before creating guest state", async () => {
  const { PassThrough } = await import("node:stream");
  const source = new PassThrough();
  const context = {
    vm: {} as any, command: () => "copy",
    run: async () => { assert.fail("stalled local source must not create guest state"); return ""; },
  };
  // Keep the loop alive while the intentionally unref'ed request timer runs.
  const keepAlive = setInterval(() => {}, 1000);
  try {
    await assert.rejects(uploadStream(context, source, "file", {timeout: .05}), DaytonaConnectionTimeoutError);
    assert.ok(source.destroyed);
  } finally { clearInterval(keepAlive); source.destroy(); }
});
