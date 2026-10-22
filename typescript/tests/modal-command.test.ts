import assert from "node:assert/strict";
import { test } from "node:test";
import { ContainerProcess } from "modal";
import { modalCommandHandle } from "../src/compat/modal-command.js";
import type { GuestProcessResult } from "../src/compat/guest-process.js";
import { GuestProcessError } from "../src/compat/guest-process.js";

test("Modal streaming stdin preserves source failures without sending EOF", async () => {
  const failure = new Error("controlled source read failure");
  const writes: Uint8Array[] = [];
  let closed = false;
  const current = {
    pid: "source",
    status: "running",
    exitCode: 0,
  } as GuestProcessResult;
  const handle = modalCommandHandle(
    {
      get: async () => current,
      readOutput: async () => ({
        data: new Uint8Array(),
        nextOffset: 0,
        eof: true,
      }),
      writeStdin: async (_id, data) => {
        writes.push(data as Uint8Array);
        return { message: "written" };
      },
      closeStdin: async () => {
        closed = true;
        return { message: "closed" };
      },
    },
    current,
  );
  await assert.rejects(
    handle._stdinWriteStream({
      async *readFrom() {
        yield new Uint8Array([1, 2]);
        throw failure;
      },
    }),
    (error) => error === failure,
  );
  assert.deepEqual(writes, [new Uint8Array([1, 2])]);
  assert.equal(closed, false);
});

test("Modal streaming stdin lets the native filesystem inspect a terminal helper's error", async () => {
  const current = {
    pid: "closed",
    status: "failed",
    exitCode: 1,
  } as GuestProcessResult;
  const closed = new GuestProcessError(
    "Process stdin is closed",
    "process_stdin_closed",
  );
  const handle = modalCommandHandle(
    {
      get: async () => current,
      readOutput: async () => ({
        data: new Uint8Array(),
        nextOffset: 0,
        eof: true,
      }),
      writeStdin: async () => {
        throw closed;
      },
      closeStdin: async () => {
        throw new Error("EOF must not be sent after a write failure");
      },
    },
    current,
  );
  assert.equal(
    await handle._stdinWriteStream({
      async *readFrom() {
        yield new Uint8Array([1]);
      },
    }),
    0,
  );
  assert.equal(await handle.wait(), 1);
});

test("Modal handles use native streaming classes, binary bytes and stdin EOF", async () => {
  const payload = Uint8Array.from({ length: 256 }, (_, index) => index);
  const writes: Uint8Array[] = [];
  let closed = false;
  const current = {
    pid: "42",
    status: "running",
    exitCode: 0,
  } as GuestProcessResult;
  const processes = {
    async get() {
      return {
        ...current,
        status: closed ? ("failed" as const) : ("running" as const),
        exitCode: 17,
      };
    },
    async readOutput(_id: string, stream: string, offset: number) {
      const value =
        stream === "stdout" ? payload : new TextEncoder().encode("diagnostic");
      const data = value.slice(offset, offset + 17);
      return {
        data,
        nextOffset: offset + data.length,
        eof: closed && offset + data.length >= value.length,
      };
    },
    async writeStdin(_id: string, data: string | Uint8Array) {
      writes.push(new Uint8Array(data as Uint8Array));
      return { message: "written" };
    },
    async closeStdin() {
      closed = true;
      return { message: "closed" };
    },
  };
  const handle = modalCommandHandle(processes, current, { mode: "binary" });
  assert.ok(handle instanceof ContainerProcess);
  await handle.stdin.writeBytes(payload);
  await handle.stdin.close();
  assert.deepEqual(writes, [payload]);
  assert.deepEqual(await handle.stdout.readBytes(), payload);
  assert.equal(await handle.stderr.readText(), "diagnostic");
  assert.equal(await handle.wait(), 17);
});

test("Modal native text decoding preserves multibyte boundaries and signed signals", async () => {
  const bytes = new TextEncoder().encode("λ🙂");
  const current = {
    pid: "43",
    status: "killed",
    exitCode: -9,
  } as GuestProcessResult;
  const processes = {
    get: async () => current,
    async readOutput(_id: string, _stream: string, offset: number) {
      const data = bytes.slice(offset, offset + 1);
      return {
        data,
        nextOffset: offset + data.length,
        eof: offset + data.length >= bytes.length,
      };
    },
    writeStdin: async () => ({ message: "written" }),
    closeStdin: async () => ({ message: "closed" }),
  };
  const handle = modalCommandHandle(processes, current);
  assert.equal(await handle.stdout.readText(), "λ🙂");
  assert.equal(await handle.wait(), 137);
});
