import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import { Volume as NativeVolume, type SandboxOpts } from "e2b";
import { ArkerVolume, Volumes } from "../src/compat/e2b-mounts.js";
import mountReadySource from "../src/compat/e2b-mount-ready.generated.js";

test("generated mount readiness matches the canonical Python source", async () => {
  assert.equal(mountReadySource, await readFile("../python/src/arker/_guest_e2b_mount_ready.py", "utf8"));
});

function fixture() {
  const calls: unknown[] = [];
  const row = (id = "fs-one", name = "data") => ({ filesystem_id: id, name, owner_org_id: "org", created_at: "2026-01-01T00:00:00Z" });
  const client = {
    async createFilesystem(request: { name: string }) { calls.push(request); return row("fs-one", request.name); },
    async getFilesystem(id: string) { calls.push(id); return row(id); },
    async listFilesystems(_opts: { cursor?: string } = {}) { return { filesystems: [row()], next_cursor: null as string | null }; },
    async deleteFilesystem(id: string) { calls.push(["delete", id]); return { deleted: true }; },
  };
  const vm = {
    async createMount(request: { filesystemId: string; path?: string }) {
      calls.push(request);
      return { mount_id: "mount-one", filesystem_id: request.filesystemId, path: request.path!, vm_id: "vm", created_at: "2026-01-01T00:00:00Z" };
    },
  };
  return { client, calls, vm, row, volumes: new Volumes(client) };
}

test("installed native API supports names, handles, and lifecycle", () => {
  const handle = new NativeVolume("native-id", "data", "token");
  const opts: SandboxOpts = { volumeMounts: { "/shared": handle, "/other": "name" } };
  assert.equal(opts.volumeMounts!["/shared"], handle);
  for (const method of [NativeVolume.create, NativeVolume.connect, NativeVolume.getInfo, NativeVolume.list, NativeVolume.destroy]) assert.equal(typeof method, "function");
});

test("create, connect, attach and destroy use filesystem APIs", async () => {
  const { volumes, vm, calls } = fixture();
  const handle = await volumes.create("data");
  assert.equal(handle.volumeId, "fs-one");
  assert.equal(handle.name, "data");
  assert.equal((await volumes.attach(vm, { "/shared": handle }))[0].mount_id, "mount-one");
  assert.equal(await volumes.destroy(handle.volumeId), true);
  assert.deepEqual(calls, [{ name: "data" }, "fs-one", { filesystemId: "fs-one", path: "/shared" }, ["delete", "fs-one"]]);
});

test("name resolution paginates exactly and never treats strings as IDs", async () => {
  const { client, volumes, row } = fixture();
  client.listFilesystems = async ({ cursor } = {}) => cursor
    ? { filesystems: [row()], next_cursor: null }
    : { filesystems: [row("fs-two", "data-extra")], next_cursor: "next" };
  assert.equal((await volumes.byName("data")).volumeId, "fs-one");
  await assert.rejects(volumes.resolve({ "/shared": "fs-one" }), /not found/);
  client.listFilesystems = async () => ({ filesystems: [row(), row("fs-two")], next_cursor: null });
  await assert.rejects(volumes.byName("data"), /ambiguous/);
  client.listFilesystems = async () => ({ filesystems: [], next_cursor: "loop" });
  await assert.rejects(volumes.list(), /cursor/);
});

test("native handles are rejected without reinterpreting their IDs or names", async () => {
  const { volumes, calls, vm } = fixture();
  const native = new NativeVolume("fs-one", "data", "token");
  await assert.rejects(volumes.attach(vm, { "/a": "data", "/b": native as unknown as ArkerVolume }), /native backend/);
  assert.deepEqual(calls, []);
});

test("invalid paths and names fail before mutation", async () => {
  const { volumes, vm, calls } = fixture();
  for (const path of ["/", "relative", "/a/../b", "/a..b", "/a//b", "/a/.", "/a/", "/\0", "/" + "é".repeat(128)]) {
    await assert.rejects(volumes.attach(vm, { "/valid": "data", [path]: "other" }), /Mount path/);
  }
  for (const name of ["", ".hidden", "trailing.", "a/b", "data\n", "é", "a".repeat(129)]) await assert.rejects(volumes.create(name), /Volume name/);
  assert.deepEqual(calls, []);
});

test("duplicates and overlap fail before mounting", async () => {
  const { volumes, vm, calls } = fixture();
  await assert.rejects(volumes.attach(vm, { "/a": "data", "/b": new ArkerVolume("fs-one", "data") }), /once per VM/);
  await assert.rejects(volumes.attach(vm, { "/a": new ArkerVolume("fs-one", "data"), "/a/b": new ArkerVolume("fs-two", "other") }), /Overlapping/);
  assert.ok(calls.every(call => typeof call === "string"));
});

test("server failures propagate and partial mounts are not detached", async () => {
  const { client, volumes } = fixture();
  const conflict = Object.assign(new Error("conflict"), { status: 409 });
  client.createFilesystem = async () => { throw conflict; };
  await assert.rejects(volumes.create("data"), error => error === conflict);
  client.deleteFilesystem = async () => { throw Object.assign(new Error("missing"), { status: 404 }); };
  assert.equal(await volumes.destroy("missing"), false);
  client.deleteFilesystem = async () => { throw conflict; };
  await assert.rejects(volumes.destroy("fs-one"), error => error === conflict);
  const { vm } = fixture();
  const original = vm.createMount;
  let attempts = 0;
  vm.createMount = async request => {
    if (++attempts === 2) throw conflict;
    return original(request);
  };
  await assert.rejects(volumes.attach(vm, { "/a": new ArkerVolume("fs-one", "data"), "/b": new ArkerVolume("fs-two", "other") }), error => error === conflict);
  assert.equal(attempts, 2);
});

test("backend store creates, mounts, reconnects and destroys", async () => {
  const { client, vm } = fixture();
  const Volume = new Volumes(client);
  const volume = await Volume.create("data");
  assert.ok(volume instanceof ArkerVolume);
  assert.equal(volume.volumeId, "fs-one");
  const mounts = { "/workspace/shared": volume };
  assert.equal((await new Volumes(client).attach(vm, mounts))[0].path, "/workspace/shared");
  const ReconnectedVolume = new Volumes(client);
  const reused = await ReconnectedVolume.connect(volume.volumeId);
  assert.equal(reused.volumeId, volume.volumeId);
  assert.equal((await Volume.getInfo(volume.volumeId)).name, "data");
  assert.equal((await Volume.list())[0].volumeId, volume.volumeId);
  assert.equal(await ReconnectedVolume.destroy(reused.volumeId), true);
});

test("backend store pins the client and keeps volume IDs opaque", async () => {
  const { client, calls } = fixture();
  const Volume = new Volumes(client);
  const vmShapedId = "01h00000000000000000000000";
  assert.equal((await Volume.connect(vmShapedId)).volumeId, vmShapedId);
  assert.deepEqual(calls, [vmShapedId]);
  const other = fixture();
  const volume = await Volume.create("data");
  await assert.rejects(other.volumes.attach(other.vm, { "/workspace/shared": volume }), /another Arker client/);
  assert.deepEqual(other.calls, []);
  const missing = Object.assign(new Error("missing"), { status: 404 });
  client.getFilesystem = async () => { throw missing; };
  await assert.rejects(Volume.connect("native-looking-id"), error => error === missing);
});

test("native handles preserve full vendor API; Arker explicitly lacks content tokens", async () => {
  const native = new NativeVolume("id", "data", "token");
  assert.equal(typeof native.getInfo, "function");
  assert.equal(native.getInfo, NativeVolume.prototype.getInfo);
  const { client } = fixture();
  const Volume = new Volumes(client);
  const volume = await Volume.create("data");
  assert.throws(() => volume.token, /tokens/);
  const metadata = await Volume.getInfo(volume.volumeId);
  assert.throws(() => metadata.token, /tokens/);
  await assert.rejects(volume.list("/"), /content API/);
  await assert.rejects(volume.getInfo("/file"), /content API/);
  await assert.rejects(volume.read("/file"), /content API/);
  await assert.rejects(volume.write("/file", "text"), /content API/);
});

test("Sandbox.create waits for mounts and deletes only the new VM on failure", async (t) => {
  const { Sandbox } = await import("../src/e2b.js");
  const { setCompatComputeFactoryForTests } = await import("../src/compat/runtime.js");
  const factory = Sandbox as unknown as { dataPlane: () => Promise<unknown> };
  const original = factory.dataPlane;
  t.after(() => { factory.dataPlane = original; });
  for (const failure of [undefined, "attach", "readiness"]) {
    const { client } = fixture();
    const events: string[] = [];
    const vm = {
      refresh: async () => ({ policies: { rules: {} } }), update: async () => {},
      _client: client,
      async createMount(request: unknown) {
        events.push("attach");
        assert.deepEqual(request, { filesystemId: "fs-one", path: "/workspace/shared" });
        if (failure === "attach") throw new Error("attach failed");
        return { mount_id: "mount-one" };
      },
      async delete() { events.push("delete"); },
    };
    const box = {
      provider: "arker", sandboxId: "child", getInstance: () => vm,
      getUrl: async () => "https://example.invalid",
      runCommand: async (command: string) => {
        const readiness = command.includes("/proc/self/mountinfo");
        events.push(readiness ? "readiness" : "identity");
        return { stdout: "", stderr: "", exitCode: readiness && failure === "readiness" ? 1 : 0 };
      },
    };
    factory.dataPlane = async () => ({ commands: { run: box.runCommand }, files: {}, git: {}, pty: {}, close() { events.push("close"); } });
    setCompatComputeFactoryForTests((() => ({ sandbox: { create: async () => box } })) as never);
    try {
      const result = Sandbox.create({ volumeMounts: { "/workspace/shared": "data" } });
      if (failure) {
        await assert.rejects(result, /failed/);
        assert.equal(events.at(-1), "delete");
        assert.equal(events.filter(event => event === "delete").length, 1);
      } else {
        assert.equal((await result).sandboxId, "child");
        assert.deepEqual(events, ["attach", "readiness"]);
      }
    } finally {
      setCompatComputeFactoryForTests();
    }
  }
});

test("backend guards reject foreign handles before sandbox creation", async () => {
  const { checkMountBackend, Volume } = await import("../src/compat/e2b-mounts.js");
  const { client } = fixture();
  const arker = await new Volumes(client).create("data");
  assert.throws(() => checkMountBackend({ "/workspace/shared": arker }, "e2b"), /Arker sandbox backend/);
  assert.throws(() => checkMountBackend({ "/workspace/shared": new NativeVolume("id", "data", "token") }, "arker"), /native E2B/);
  assert.equal(typeof Volume.create, "function");
});

test("mount readiness checks kernel state rather than directory existence", async () => {
  const { execSync } = await import("node:child_process");
  const { mountReadyCommand } = await import("../src/compat/e2b-mounts.js");
  assert.throws(() => execSync(mountReadyCommand(["/tmp"], 0), { stdio: "pipe" }), /readiness timed out/);
});

test("native sandbox provider receives the original volume handles", async (t) => {
  const { Sandbox: NativeSandbox } = await import("e2b");
  const { Sandbox } = await import("../src/e2b.js");
  const { preserveEnvironment, preserveProperties } = await import("./provider-fixtures.js");
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  preserveProperties(t, NativeSandbox, "create");
  const handle = new NativeVolume("native-volume", "data", "token");
  const mounts = { "/workspace/shared": handle };
  NativeSandbox.create = (async (options: { volumeMounts: unknown }) => {
    assert.equal(options.volumeMounts, mounts);
    return { sandboxId: "native-child", files: {}, commands: {} };
  }) as never;
  assert.equal((await Sandbox.create({ volumeMounts: mounts })).sandboxId, "native-child");
});

async function volumeRoute(t: import("node:test").TestContext, order: string[], create?: unknown) {
  const { mkdtemp, writeFile, rm } = await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { preserveEnvironment } = await import("./provider-fixtures.js");
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_BASE_URL", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "synthetic";
  process.env.ARKER_BASE_URL = "https://example.invalid";
  const directory = await mkdtemp(join(tmpdir(), "volume-router-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const cache = join(directory, "routes.json");
  const path = join(directory, "router.json");
  await writeFile(path, JSON.stringify({
    version: 1, existing: { strategy: "priority", order },
    create: create ?? { strategy: "priority", order }, cache: { path: cache },
  }));
  process.env.COMPUTESDK_ROUTER_CONFIG = path;
  return cache;
}

test("import-only Volume lifecycle routes to Arker and namespaces its cache", async (t) => {
  const { Volume } = await import("../src/e2b.js");
  const { Arker } = await import("../src/index.js");
  const { readFile } = await import("node:fs/promises");
  const { preserveProperties } = await import("./provider-fixtures.js");
  const cache = await volumeRoute(t, ["arker"]);
  preserveProperties(t, Arker.prototype, "createFilesystem", "getFilesystem", "listFilesystems", "deleteFilesystem");
  const { client, calls } = fixture();
  Arker.prototype.createFilesystem = client.createFilesystem;
  Arker.prototype.getFilesystem = client.getFilesystem;
  Arker.prototype.listFilesystems = client.listFilesystems;
  Arker.prototype.deleteFilesystem = client.deleteFilesystem;
  await assert.rejects(Volume.create("data", { apiKey: "different-route" }), /per-call/);
  assert.deepEqual(calls, []);
  const volume = await Volume.create("data");
  assert.ok(volume instanceof ArkerVolume);
  assert.equal((await Volume.connect(volume.volumeId)).volumeId, volume.volumeId);
  assert.equal((await Volume.getInfo(volume.volumeId)).name, "data");
  assert.equal((await Volume.list()).length, 1);
  assert.equal(await Volume.destroy(volume.volumeId), true);
  assert.deepEqual(calls.at(-1), ["delete", "fs-one"]);
  const entries = JSON.parse(await readFile(cache, "utf8")).entries;
  assert.equal(entries["e2b-volume:fs-one"].provider, "arker");
  assert.equal(entries["fs-one"], undefined);
  assert.equal(entries["data"], undefined);
});

test("native volume ownership ignores VM grammar and unrelated VM cache entries", async (t) => {
  const { Volume } = await import("../src/e2b.js");
  const { readFile, writeFile } = await import("node:fs/promises");
  const { preserveProperties } = await import("./provider-fixtures.js");
  const cache = await volumeRoute(t, ["e2b", "arker"]);
  const id = "01h00000000000000000000000";
  await writeFile(cache, JSON.stringify({ version: 1, entries: { [id]: { provider: "arker", accessedAt: 1 } } }));
  preserveProperties(t, NativeVolume, "create", "connect", "getInfo", "destroy");
  const native = new NativeVolume(id, "data", "token");
  NativeVolume.create = (async () => native) as never;
  NativeVolume.connect = (async () => native) as never;
  NativeVolume.getInfo = async () => ({ volumeId: id, name: "data", token: "token" });
  NativeVolume.destroy = async () => true;
  assert.equal(await Volume.create("data"), native);
  assert.equal(await Volume.connect(id), native);
  assert.equal((await Volume.getInfo(id)).name, "data");
  assert.equal(await Volume.destroy(id), true);
  const entries = JSON.parse(await readFile(cache, "utf8")).entries;
  assert.equal(entries[id].provider, "arker");
  assert.equal(entries[`e2b-volume:${id}`].provider, "e2b");
});

test("volume lookup falls through only on explicit not-found", async (t) => {
  const { VolumeNotFoundError } = await import("e2b");
  const { Volume } = await import("../src/e2b.js");
  const { Arker } = await import("../src/index.js");
  const { preserveProperties } = await import("./provider-fixtures.js");
  await volumeRoute(t, ["e2b", "arker"]);
  preserveProperties(t, NativeVolume, "connect");
  preserveProperties(t, Arker.prototype, "getFilesystem");
  let arkerCalls = 0;
  const { row } = fixture();
  Arker.prototype.getFilesystem = async id => { arkerCalls++; return row(id); };
  const denied = Object.assign(new Error("denied"), { status: 403 });
  NativeVolume.connect = async () => { throw denied; };
  await assert.rejects(Volume.connect("id"), error => error === denied);
  assert.equal(arkerCalls, 0);
  NativeVolume.connect = async () => { throw new VolumeNotFoundError("missing"); };
  assert.ok(await Volume.connect("id") instanceof ArkerVolume);
  assert.equal(arkerCalls, 1);
});

test("weighted volume creation follows policy and never retries an uncertain remote create", async (t) => {
  const { Volume } = await import("../src/e2b.js");
  const { Arker } = await import("../src/index.js");
  const { preserveProperties } = await import("./provider-fixtures.js");
  await volumeRoute(t, ["arker", "e2b"], { strategy: "weighted", weights: { arker: 1 }, fallback: ["e2b"] });
  preserveProperties(t, Arker.prototype, "createFilesystem");
  preserveProperties(t, NativeVolume, "create");
  const failure = Object.assign(new Error("uncertain"), { status: 503 });
  Arker.prototype.createFilesystem = async () => { throw failure; };
  NativeVolume.create = async () => { assert.fail("Do not retry remote create"); };
  await assert.rejects(Volume.create("data"), error => error === failure);
});

test("volume listing follows configured backends and propagates failures", async (t) => {
  const { Volume } = await import("../src/e2b.js");
  const { Arker } = await import("../src/index.js");
  const { preserveProperties } = await import("./provider-fixtures.js");
  await volumeRoute(t, ["arker", "e2b"]);
  preserveProperties(t, Arker.prototype, "listFilesystems");
  preserveProperties(t, NativeVolume, "list");
  Arker.prototype.listFilesystems = fixture().client.listFilesystems;
  const native = { volumeId: "native-id", name: "data" };
  NativeVolume.list = async () => [native];
  const listed = await Volume.list();
  assert.ok(listed[0] instanceof ArkerVolume);
  assert.equal(listed[1], native);
  const denied = new Error("list denied");
  NativeVolume.list = async () => { throw denied; };
  await assert.rejects(Volume.list(), error => error === denied);
});

test("independently routed clients with the same scope can mount multiple volumes", async () => {
  const { Arker } = await import("../src/index.js");
  const { mountStore } = await import("../src/compat/e2b-mounts.js");
  const first = new Arker({ apiKey: "synthetic", baseUrl: "https://example.invalid" });
  const second = new Arker({ apiKey: "synthetic", baseUrl: "https://example.invalid" });
  first.getFilesystem = fixture().client.getFilesystem;
  const mounts = { "/a": new ArkerVolume("fs-one", "data", first), "/b": new ArkerVolume("fs-two", "other", second) };
  assert.equal(Object.keys(await mountStore(first, mounts).resolve(mounts)).length, 2);
});
