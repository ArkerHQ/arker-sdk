import assert from "node:assert/strict";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  SandboxFilesystem,
  SandboxFilesystemDirectoryNotEmptyError,
  SandboxFilesystemNotFoundError,
} from "modal";
import { Sandbox } from "../src/modal.js";
import { modalCommandHandle } from "../src/compat/modal-command.js";
import { modalFilesystem } from "../src/compat/modal-filesystem.js";

test("Modal sandbox filesystem is lazy on Arker and exact native passthrough elsewhere", () => {
  const native = { filesystem: {} };
  const routed = new Sandbox({
    sandboxId: "native",
    provider: "modal",
    getInstance: () => native,
  } as never);
  assert.equal(routed.filesystem, native.filesystem);
  const arker = new Sandbox({ sandboxId: "owned", provider: "arker" } as never);
  assert.ok(arker.filesystem instanceof SandboxFilesystem);
  assert.equal(arker.filesystem, arker.filesystem);
});

test("Modal's native filesystem frontend preserves bytes, metadata and local copies", async () => {
  const root = await mkdtemp(join(tmpdir(), "arker-modal-fs-"));
  const children: ChildProcessWithoutNullStreams[] = [];
  const filesystem = modalFilesystem(async (command, options) => {
    const process = spawn(command[0]!, command.slice(1));
    children.push(process);
    const chunks: Record<string, Buffer[]> = { stdout: [], stderr: [] };
    let exitCode: number | null = null;
    process.stdout.on("data", (data) => chunks.stdout!.push(Buffer.from(data)));
    process.stderr.on("data", (data) => chunks.stderr!.push(Buffer.from(data)));
    process.on("close", (code) => {
      exitCode = code ?? 128;
    });
    const current = () => ({
      pid: String(process.pid),
      status: exitCode === null ? ("running" as const) : ("completed" as const),
      exitCode: exitCode ?? 0,
    });
    const transport = {
      get: async () => current(),
      async readOutput(
        _id: string,
        stream: "stdout" | "stderr",
        offset: number,
      ) {
        const data = Buffer.concat(chunks[stream]!).subarray(offset);
        return {
          data,
          nextOffset: offset + data.length,
          eof: exitCode !== null,
        };
      },
      async writeStdin(_id: string, data: string | Uint8Array) {
        await new Promise<void>((resolve, reject) =>
          process.stdin.write(data, (error) =>
            error ? reject(error) : resolve(),
          ),
        );
        return { message: "written" };
      },
      async closeStdin() {
        await new Promise<void>((resolve) => process.stdin.end(resolve));
        return { message: "closed" };
      },
    };
    return modalCommandHandle(
      transport as never,
      current() as never,
      options as never,
    );
  });
  try {
    const path = join(root, "remote");
    await filesystem.makeDirectory(path);
    const payload = Uint8Array.from(
      { length: 256000 },
      (_, index) => index % 256,
    );
    await filesystem.writeBytes(payload, join(path, "binary"));
    assert.deepEqual(await filesystem.readBytes(join(path, "binary")), payload);
    await filesystem.writeText("hello λ\n", join(path, "nested/text"));
    assert.equal(
      await filesystem.readText(join(path, "nested/text")),
      "hello λ\n",
    );
    const info = await filesystem.stat(join(path, "binary"));
    assert.equal(info.type, "file");
    assert.equal(info.size, payload.length);
    assert.equal(info.permissions, "0644");
    assert.equal(typeof info.modifiedTime, "number");
    assert.deepEqual(
      (await filesystem.listFiles(path)).map((entry) => entry.name),
      ["binary", "nested"],
    );
    const local = join(root, "download/binary");
    await filesystem.copyToLocal(join(path, "binary"), local);
    assert.deepEqual(new Uint8Array(await readFile(local)), payload);
    await filesystem.copyFromLocal(local, join(path, "uploaded"));
    assert.deepEqual(
      await filesystem.readBytes(join(path, "uploaded")),
      payload,
    );
    await assert.rejects(
      filesystem.remove(path),
      SandboxFilesystemDirectoryNotEmptyError,
    );
    await filesystem.remove(path, { recursive: true });
    await assert.rejects(filesystem.stat(path), SandboxFilesystemNotFoundError);
    assert.throws(() => filesystem.watch(path), /ordered guest event stream/);
  } finally {
    for (const child of children)
      if (child.exitCode === null) child.kill("SIGKILL");
    await rm(root, { recursive: true, force: true });
  }
});
