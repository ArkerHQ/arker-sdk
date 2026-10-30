import assert from "node:assert/strict";
import { test } from "node:test";
import { VM } from "../src/index.js";

test("stream reads close presigned response on early return without buffering", async () => {
  let cancelled = false;
  let reads = 0;
  const signal = new AbortController().signal;
  const vm = new VM({
    _request: async (...args: unknown[]) => {
      assert.equal((args[7] as { signal: AbortSignal }).signal, signal);
      return { presigned_url: "https://storage.invalid/file" };
    },
    _download: async (_url: string, options: { signal: AbortSignal }) => {
      assert.equal(options.signal, signal);
      return new Response(new ReadableStream({
        pull(controller) { reads++; controller.enqueue(new Uint8Array(65536)); },
        cancel() { cancelled = true; },
      }, { highWaterMark: 0 }));
    },
  } as any, "vm-test", "https://api.invalid");
  const chunks = vm._syncReadChunks("/file", signal);
  assert.equal((await chunks.next()).value?.length, 65536);
  await chunks.return(undefined);
  assert.equal(reads, 1);
  assert.equal(cancelled, true);
});

test("stream upload propagates cancellation into pending sync request", async () => {
  const controller = new AbortController();
  const reason = new Error("cancel pending upload");
  let started!: () => void;
  const ready = new Promise<void>(resolve => { started = resolve; });
  const vm = new VM({
    _retryAttempts: () => 1,
    _request: async (...args: unknown[]) => {
      const signal = (args[7] as { signal: AbortSignal }).signal;
      assert.equal(signal, controller.signal);
      started();
      return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
    },
  } as any, "vm-test", "https://api.invalid");
  const transfer = vm._syncWriteChunks("/file", 1, async () => new Uint8Array([1]), {}, { signal: controller.signal });
  await ready;
  controller.abort(reason);
  await assert.rejects(transfer, error => error === reason);
});
