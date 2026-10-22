import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { test } from "node:test";
import {
  Sandbox as Native,
  NotFoundError,
  CommandExitError,
  FileType,
} from "e2b";
import { Sandbox } from "../src/e2b.js";
import { createNativeE2BProvider } from "../src/compat/e2b-provider.js";

test("native filesystem handles preserve binary options and write metadata", async () => {
  const marker = { path: "/binary", name: "binary" };
  class NativeFiles {
    async read(path: string, options: unknown) {
      assert.equal(path, "/binary");
      assert.deepEqual(options, { format: "bytes", user: "user", requestTimeoutMs: 7000 });
      return new Uint8Array([0, 255, 128]);
    }
    async write(path: string, content: unknown, options: unknown) {
      assert.equal(path, "/binary");
      assert.deepEqual(content, new Uint8Array([255]).buffer);
      assert.deepEqual(options, { user: "user" });
      return marker;
    }
  }
  const files = new NativeFiles();
  const sandbox = Reflect.construct(Sandbox, [
    { provider: "e2b", sandboxId: "native", filesystem: { readFile: async () => "translated" } },
    { files },
  ]) as Sandbox;
  assert.equal(sandbox.files, files);
  assert.deepEqual(await sandbox.files.read("/binary", { format: "bytes", user: "user", requestTimeoutMs: 7000 }), new Uint8Array([0, 255, 128]));
  assert.equal(await sandbox.files.write("/binary", new Uint8Array([255]).buffer, { user: "user" }), marker);
});

test("Arker filesystem options are rejected instead of silently ignored", async () => {
  let calls = 0;
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "arker",
    filesystem: {
      readFile: async () => { calls++; return "wrong"; },
      writeFile: async () => { calls++; },
      mkdir: async () => { calls++; },
      readdir: async () => { calls++; return []; },
      exists: async () => { calls++; return false; },
      remove: async () => { calls++; },
    },
  }]) as Sandbox;
  for (const method of ["read", "list", "exists", "remove", "makeDir"] as const) {
    await assert.rejects(sandbox.files[method]("/owned", { user: "another" }), /received unsupported option\(s\) 'user'/);
  }
  await assert.rejects(sandbox.files.write("/owned", "data", { user: "another" }), /received unsupported option\(s\) 'user'/);
  assert.equal(calls, 0);
});

test("Arker binary reads preserve every byte and the native Uint8Array type", async () => {
  const payload = Uint8Array.from({ length: 256 }, (_, index) => index);
  let calls = 0;
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "arker",
    runCommand: async () => ({ stdout: "", stderr: "", exitCode: 0 }),
    getInstance: () => ({ sync: async (path: string, data?: Uint8Array) => {
      calls++;
      if (data !== undefined) {
        assert.match(path, /^\/tmp\/\.computesdk-e2b-write-/);
        assert.deepEqual(data, payload);
        return;
      }
      assert.equal(path, "/binary");
      return Buffer.from(payload);
    } }),
  }]) as Sandbox;
  const result = await sandbox.files.read("/binary", { format: "bytes" });
  assert.equal(result.constructor, Uint8Array);
  assert.deepEqual(result, payload);
  assert.equal(calls, 1);
  await sandbox.files.write("/binary", payload.buffer);
  assert.equal(calls, 2);
  await assert.rejects(sandbox.files.read("/binary", { format: "stream" }), /format/);
  assert.equal(calls, 2);
});

test("Arker writes and metadata match the E2B default user", async () => {
  const commands: string[] = [];
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "arker",
    filesystem: { writeFile: async () => undefined },
    getInstance: () => ({ sync: async () => undefined }),
    runCommand: async (command: string) => {
      commands.push(command);
      return {
        stdout: command.startsWith("stat ") ? "/tmp/file\x1f4\x1f81a4\x1f-rw-r--r--\x1fuser\x1fuser\x1f1\x1fregular file" : "",
        stderr: "", exitCode: 0,
      };
    },
  }]) as Sandbox;
  await sandbox.files.write("/tmp/file", "data");
  const info = await sandbox.files.getInfo("/tmp/file");
  assert.match(commands[0]!, /^runuser -u user -- sh -c /);
  assert.match(commands[0]!, /cat -- \/tmp\/.computesdk-e2b-write-/);
  assert.match(commands[0]!, /> \/tmp\/file'$/);
  assert.match(commands[1]!, /^rm -f -- \/tmp\/.computesdk-e2b-write-/);
  assert.equal(info.permissions, "-rw-r--r--");
  assert.deepEqual([info.owner, info.group], ["user", "user"]);
});

test("Arker writes preserve user permission failures and clean staging", async () => {
  const commands: string[] = [];
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "arker",
    getInstance: () => ({ sync: async () => undefined }),
    runCommand: async (command: string) => {
      commands.push(command);
      return { stdout: "", stderr: command.startsWith("runuser ") ? "Permission denied" : "", exitCode: command.startsWith("runuser ") ? 1 : 0 };
    },
  }]) as Sandbox;
  await assert.rejects(sandbox.files.write("/root/owned", "data"), /Permission denied/);
  assert.match(commands[0]!, /^runuser -u user -- /);
  assert.match(commands[1]!, /^rm -f -- \/tmp\/.computesdk-e2b-write-/);
});

test("Arker writes clean partial staging after sync failure", async () => {
  const commands: string[] = [];
  const sandbox = Reflect.construct(Sandbox, [{
    provider: "arker", sandboxId: "arker",
    getInstance: () => ({ sync: async () => { throw new Error("upload interrupted"); } }),
    runCommand: async (command: string) => {
      commands.push(command);
      return { stdout: "", stderr: "", exitCode: 0 };
    },
  }]) as Sandbox;
  await assert.rejects(sandbox.files.write("/tmp/file", "data"), /upload interrupted/);
  assert.equal(commands.length, 1);
  assert.match(commands[0]!, /^rm -f -- \/tmp\/.computesdk-e2b-write-/);
});

test("the shim provider preserves embedding SDK identity, native filesystem metadata and errors", async () => {
  const original = { create: Native.create, connect: Native.connect };
  const modifiedTime = new Date("2026-10-17T00:00:00Z");
  const failure = new CommandExitError({
    stdout: "output",
    stderr: "failure",
    exitCode: 17,
    error: "exit status 17",
  });
  const native = {
    sandboxId: "embedded",
    commands: {
      run: async () => {
        throw failure;
      },
    },
    files: {
      list: async () => [
        { name: "folder", type: FileType.DIR, size: 4096, modifiedTime },
      ],
    },
  } as unknown as Native;
  Native.create = (async () => native) as typeof Native.create;
  Native.connect = (async () => native) as typeof Native.connect;
  try {
    const provider = createNativeE2BProvider();
    const box = await provider.instances.create();
    assert.equal(box.provider, "e2b");
    assert.equal(box.getInstance(), native);
    assert.equal(
      (await provider.instances.getById("embedded"))!.getInstance(),
      native,
    );
    assert.deepEqual(await box.filesystem.readdir("/"), [
      { name: "folder", type: "directory", size: 4096, modified: modifiedTime },
    ]);
    const result = await box.runCommand("exit 17");
    assert.deepEqual(
      [result.stdout, result.stderr, result.exitCode],
      ["output", "failure", 17],
    );
    const unavailable = new Error("fetch failed");
    Native.create = async () => {
      throw unavailable;
    };
    Native.connect = async () => {
      throw unavailable;
    };
    await assert.rejects(
      provider.instances.create(),
      (error) => error === unavailable,
    );
    await assert.rejects(
      provider.instances.getById("embedded"),
      (error) => error === unavailable,
    );
    Native.connect = async () => {
      throw new NotFoundError("missing");
    };
    assert.equal(await provider.instances.getById("missing"), null);
  } finally {
    Native.create = original.create;
    Native.connect = original.connect;
  }
});

test("static E2B deletion uses metadata ownership without connecting and preserves errors", async () => {
  const previous = {
    info: Native.getInfo,
    kill: Native.kill,
    connect: Native.connect,
  };
  const environment = {
    key: process.env.ARKER_API_KEY,
    router: process.env.COMPUTESDK_ROUTER_CONFIG,
  };
  delete process.env.ARKER_API_KEY;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  let probes = 0;
  let deletes = 0;
  const unavailable = new Error("forbidden");
  Native.connect = async () => {
    throw new Error("Deletion must not connect");
  };
  Native.getInfo = async () => {
    probes++;
    if (probes === 2) throw new NotFoundError("missing");
    if (probes === 3) throw unavailable;
    return {} as Awaited<ReturnType<typeof Native.getInfo>>;
  };
  Native.kill = async (id) => {
    assert.equal(id, "native-id");
    deletes++;
    return true;
  };
  try {
    assert.equal(await Sandbox.kill("native-id"), true);
    assert.equal(await Sandbox.kill("native-id"), false);
    await assert.rejects(
      Sandbox.kill("native-id"),
      (error) => error === unavailable,
    );
    assert.equal(deletes, 1);
  } finally {
    Native.getInfo = previous.info;
    Native.kill = previous.kill;
    Native.connect = previous.connect;
    if (environment.key === undefined) delete process.env.ARKER_API_KEY;
    else process.env.ARKER_API_KEY = environment.key;
    if (environment.router === undefined)
      delete process.env.COMPUTESDK_ROUTER_CONFIG;
    else process.env.COMPUTESDK_ROUTER_CONFIG = environment.router;
  }
});

test("static Arker deletion translates idempotent DELETE without probing incompatible native IDs", async () => {
  const id = "vmh-b471cad989f2eab9-01M548DGAXJ48SGE3SRTRR8HNV";
  let deleted = false;
  let deletes = 0;
  const server = createServer((request, response) => {
    assert.equal(request.headers.authorization, "Bearer ark_test");
    assert.equal(request.url, `/api/v1/vms/${id}`);
    response.setHeader("content-type", "application/json");
    if (request.method === "DELETE") {
      deleted = true;
      deletes++;
      response.end(JSON.stringify({ deleted: true }));
    } else if (deleted) {
      response.writeHead(404);
      response.end(
        JSON.stringify({ error: { code: "not_found", message: "missing" } }),
      );
    } else {
      response.end(
        JSON.stringify({
          vm_id: id,
          owner_org_id: "owner",
          created_at: "2026-10-17T00:00:00Z",
          public: false,
          state: "idle",
          sessions: [],
          network: {},
          resources: {},
          hostname: "vm.example.invalid",
        }),
      );
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address === "object");
  const previous = {
    key: process.env.ARKER_API_KEY,
    base: process.env.ARKER_BASE_URL,
    router: process.env.COMPUTESDK_ROUTER_CONFIG,
    info: Native.getInfo,
  };
  process.env.ARKER_API_KEY = "ark_test";
  process.env.ARKER_BASE_URL = `http://127.0.0.1:${address.port}/api`;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  Native.getInfo = async () => {
    throw new Error("Arker IDs must not be sent to E2B");
  };
  try {
    assert.equal(await Sandbox.kill(id), true);
    assert.equal(await Sandbox.kill(id), false);
    assert.equal(deletes, 1);
  } finally {
    Native.getInfo = previous.info;
    for (const [key, value] of [
      ["ARKER_API_KEY", previous.key],
      ["ARKER_BASE_URL", previous.base],
      ["COMPUTESDK_ROUTER_CONFIG", previous.router],
    ]) {
      if (value === undefined) delete process.env[key!];
      else process.env[key!] = value;
    }
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
