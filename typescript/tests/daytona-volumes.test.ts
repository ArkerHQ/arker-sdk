import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { DaytonaAuthenticationError, DaytonaForbiddenError, DaytonaNotFoundError } from "@daytona/sdk";

import { Arker, ArkerError } from "../src/index.js";
import { Daytona } from "../src/daytona.js";
import { DaytonaVolumeService } from "../src/compat/daytona-volumes.js";
import { preserveEnvironment, preserveProperties } from "./provider-fixtures.js";

const row = (id = "dir_one", name = "shared") => ({
  filesystem_id: id,
  name,
  owner_org_id: "org-one",
  created_at: "2026-10-29T00:00:00Z",
});

function nativeService(values: any[] = []) {
  const calls: unknown[] = [];
  return {
    calls,
    service: {
      async create(name: string) { calls.push(["create", name]); return { id: "native-one", name }; },
      async get(name: string) {
        calls.push(["get", name]);
        const found = values.find((volume) => volume.name === name);
        if (!found) throw Object.assign(new Error("missing"), { statusCode: 404 });
        return found;
      },
      async list() { calls.push(["list"]); return values; },
      async delete(volume: unknown) { calls.push(["delete", volume]); },
    },
  };
}

function patchArker(t: import("node:test").TestContext) {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  preserveProperties(t, Arker.prototype, "createFilesystem", "getFilesystem", "listFilesystems");
  process.env.ARKER_API_KEY = "arker-test";
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
}

test("Arker-only Daytona clients omit an unconfigured native volume provider", async (t) => {
  patchArker(t);
  preserveEnvironment(t, "DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID");
  delete process.env.DAYTONA_API_KEY;
  delete process.env.DAYTONA_JWT_TOKEN;
  delete process.env.DAYTONA_ORGANIZATION_ID;
  Arker.prototype.listFilesystems = async () => ({ filesystems: [row()], next_cursor: null }) as never;
  const client = new Daytona({ arker: { apiKey: "arker-test" } });
  assert.deepEqual((await client.volume.list()).map((volume) => volume.id), ["dir_one"]);
  await client[Symbol.asyncDispose]();
});

test("zero eligible providers preserve Daytona's native missing-credentials error", (t) => {
  preserveEnvironment(t, "ARKER_API_KEY", "DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID");
  delete process.env.ARKER_API_KEY;
  delete process.env.DAYTONA_API_KEY;
  delete process.env.DAYTONA_JWT_TOKEN;
  delete process.env.DAYTONA_ORGANIZATION_ID;
  assert.throws(() => new Daytona(), (error: unknown) => (
    error instanceof DaytonaAuthenticationError && error.statusCode === undefined
  ));
});

test("explicit routers reject an unavailable native volume provider", async (t) => {
  patchArker(t);
  preserveEnvironment(t, "DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID");
  delete process.env.DAYTONA_API_KEY;
  delete process.env.DAYTONA_JWT_TOKEN;
  delete process.env.DAYTONA_ORGANIZATION_ID;
  const directory = await mkdtemp(join(tmpdir(), "daytona-volume-eligibility-"));
  const config = join(directory, "router.json");
  await writeFile(config, JSON.stringify({
    version: 1,
    existing: { strategy: "priority", order: ["daytona"] },
    create: { strategy: "priority", order: ["daytona"] },
  }));
  const client = new Daytona({ arker: { apiKey: "arker-test" }, routerConfigPath: config });
  const results = await Promise.allSettled([
    client.volume.list(),
    client.volume.assertNativeMounts([{ volumeId: "native", mountPath: "/data" }]),
  ]);
  assert.equal(results.every((result) => result.status === "rejected" && /unknown provider "daytona"/.test(String(result.reason))), true);
  await client[Symbol.asyncDispose]();
});

test("native volume eligibility follows Daytona API-key and JWT environment auth", async (t) => {
  preserveEnvironment(t, "ARKER_API_KEY", "DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID");
  delete process.env.ARKER_API_KEY;
  process.env.DAYTONA_API_KEY = "invalid-but-configured";
  delete process.env.DAYTONA_JWT_TOKEN;
  delete process.env.DAYTONA_ORGANIZATION_ID;
  const apiKeyClient = new Daytona();
  await apiKeyClient[Symbol.asyncDispose]();

  delete process.env.DAYTONA_API_KEY;
  process.env.DAYTONA_JWT_TOKEN = "invalid-but-configured";
  process.env.DAYTONA_ORGANIZATION_ID = "org-test";
  const jwtClient = new Daytona();
  await jwtClient[Symbol.asyncDispose]();
});

test("partial JWT configuration remains eligible and propagates the native organization error", async (t) => {
  patchArker(t);
  preserveEnvironment(t, "DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID");
  delete process.env.DAYTONA_API_KEY;
  process.env.DAYTONA_JWT_TOKEN = "configured-jwt";
  delete process.env.DAYTONA_ORGANIZATION_ID;
  Arker.prototype.listFilesystems = async () => ({ filesystems: [], next_cursor: null }) as never;
  const client = new Daytona({ arker: { apiKey: "arker-test" } });
  await assert.rejects(client.volume.get("missing"), DaytonaAuthenticationError);
  await client[Symbol.asyncDispose]();
});

test("native authentication failures propagate instead of becoming absence", async (t) => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const native = nativeService();
  native.service.get = async () => { throw new DaytonaAuthenticationError("invalid key", 401); };
  const service = new DaytonaVolumeService(undefined, () => native.service as never);
  await assert.rejects(service.get("missing"), DaytonaAuthenticationError);
  await assert.rejects(service.get("missing", true), DaytonaAuthenticationError);
});

test("native-only volume clients do not require Arker credentials", async (t) => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY;
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const native = nativeService([{ id: "native-one", name: "shared" }]);
  const service = new DaytonaVolumeService(undefined, () => native.service as never);
  assert.equal((await service.get("shared")).id, "native-one");
  await assert.rejects(service.get("native-one"), DaytonaNotFoundError);
  const calls = native.calls.length;
  await service.assertNativeMounts([{ volumeId: "native-one", mountPath: "/mnt/shared" }]);
  assert.equal(native.calls.length, calls, "native-only mount preflight must pass through without lookup");
  await service.delete(await service.get("shared"));
  const unbound = { id: "native-unbound", name: "external" };
  await service.delete(unbound as never);
  assert.deepEqual(native.calls.at(-1), ["delete", unbound]);
  const otherNative = nativeService();
  const other = new DaytonaVolumeService(undefined, () => otherNative.service as never);
  const owned = await service.get("shared");
  await other.delete(owned);
  assert.deepEqual(native.calls.at(-1), ["delete", owned]);
  assert.equal(otherNative.calls.length, 0);
  await assert.rejects(service.delete({ id: "dir_forged", name: "forged" } as never), /provider-bound handle/);
  await assert.rejects(service.resolveMounts([{ volumeId: "dir_arker", mountPath: "/mnt/shared" }]), /require an Arker sandbox/);
});

test("public name lookup never treats provider IDs as names", async (t) => {
  patchArker(t);
  Arker.prototype.listFilesystems = async () => ({ filesystems: [row("opaque-id", "shared")], next_cursor: null }) as never;
  Arker.prototype.getFilesystem = async (id) => row(id, "shared") as never;
  const native = nativeService([{ id: "native-id", name: "native-name" }]);
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => native.service as never);
  await assert.rejects(service.get("opaque-id"), DaytonaNotFoundError);
  await assert.rejects(service.get("native-id"), DaytonaNotFoundError);
  assert.ok(native.calls.some((call: any) => call[0] === "get" && call[1] === "native-id"));
});

test("native mount passthrough follows the configured existing-provider order", async (t) => {
  patchArker(t);
  let arkerLists = 0;
  Arker.prototype.listFilesystems = async () => { arkerLists += 1; return { filesystems: [row()], next_cursor: null } as never; };
  const directory = await mkdtemp(join(tmpdir(), "daytona-volume-router-"));
  const config = join(directory, "router.json");
  await writeFile(config, JSON.stringify({
    version: 1,
    existing: { strategy: "priority", order: ["daytona"] },
    create: { strategy: "priority", order: ["daytona"] },
  }));
  const native = nativeService();
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => native.service as never, config);
  await service.assertNativeMounts([{ volumeId: "native-opaque", mountPath: "/mnt/shared" }]);
  assert.equal(arkerLists, 0);
  assert.equal(native.calls.length, 0);
});

test("Daytona volume create/get/list expose native fields and retain provider identity", async (t) => {
  patchArker(t);
  const pages: Array<{ filesystems: ReturnType<typeof row>[]; next_cursor: string | null }> = [
    { filesystems: [row("dir_one", "shared")], next_cursor: "next" },
    { filesystems: [row("dir_two", "other")], next_cursor: null },
  ];
  Arker.prototype.createFilesystem = async ({ name }) => row("dir_created", name) as never;
  Arker.prototype.listFilesystems = async ({ cursor } = {}) => pages[cursor ? 1 : 0] as never;
  Arker.prototype.getFilesystem = async (id) => row(id, id === "dir_one" ? "shared" : "other") as never;
  const native = nativeService();
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => native.service as never);

  const created = await service.create("name accepted by backend");
  assert.deepEqual(created, {
    id: "dir_created", name: "name accepted by backend", organizationId: "org-one", state: "ready",
    createdAt: "2026-10-29T00:00:00Z", updatedAt: "2026-10-29T00:00:00Z", lastUsedAt: null, errorReason: null,
  });
  assert.equal((await service.get("shared")).id, "dir_one");
  assert.deepEqual((await service.list()).map((volume) => volume.id), ["dir_one", "dir_two"]);
  await assert.rejects(service.delete(created), /cannot atomically refuse mounted volumes/);
  const unbound = { id: "native-one", name: "native" };
  await service.delete(unbound as never);
  assert.deepEqual(native.calls.at(-1), ["delete", unbound]);
});

test("unique name lookup propagates auth failures and rejects cross-provider ambiguity", async (t) => {
  patchArker(t);
  Arker.prototype.listFilesystems = async () => ({ filesystems: [row()], next_cursor: null }) as never;
  Arker.prototype.getFilesystem = async () => row() as never;
  const native = nativeService([{ id: "native-one", name: "shared" }]);
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => native.service as never);
  await assert.rejects(service.get("shared"), /ambiguous across configured providers/);

  native.service.get = async () => { throw new DaytonaForbiddenError("denied", 403); };
  await assert.rejects(service.get("shared"), DaytonaForbiddenError);
  await assert.rejects(service.get("shared", true), DaytonaForbiddenError);
});

test("terminal 500-row filesystem pages fail closed before fallback or create", async (t) => {
  patchArker(t);
  Arker.prototype.listFilesystems = async () => ({
    filesystems: Array.from({ length: 500 }, (_, index) => row(`dir_${index}`, `volume-${index}`)),
    next_cursor: null,
  }) as never;
  const native = nativeService();
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => native.service as never);
  await assert.rejects(service.get("missing"), /may be truncated/);
  await assert.rejects(service.get("missing", true), /may be truncated/);
  assert.ok(native.calls.every((call: any) => call[0] === "get"));
});

test("mount resolution is whole-filesystem, provider-bound and fail-closed", async (t) => {
  patchArker(t);
  Arker.prototype.listFilesystems = async () => ({ filesystems: [row()], next_cursor: null }) as never;
  Arker.prototype.getFilesystem = async (id) => row(id) as never;
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => nativeService().service as never);
  assert.deepEqual(await service.resolveMounts([{ volumeId: "shared", mountPath: "/mnt/shared" }]), [
    { filesystemId: "dir_one", mountPath: "/mnt/shared" },
  ]);
  assert.deepEqual(await service.resolveMounts([{ volumeId: "dir_direct", mountPath: "/data" }]), [
    { filesystemId: "dir_direct", mountPath: "/data" },
  ]);
  await assert.rejects(service.resolveMounts([{ volumeId: "shared", mountPath: "/mnt", subpath: "child" }]), /do not support subpath/);
  await assert.rejects(service.resolveMounts([{ volumeId: "shared", mountPath: "relative" }]), /canonical, absolute/);
  await assert.rejects(service.assertNativeMounts([{ volumeId: "shared", mountPath: "/mnt/shared" }]), /require an Arker sandbox/);
});

test("invalid subpath runtime types fail before provider I/O", async (t) => {
  patchArker(t);
  let arkerLists = 0;
  Arker.prototype.listFilesystems = async () => { arkerLists += 1; return { filesystems: [row()], next_cursor: null } as never; };
  const native = nativeService();
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => native.service as never);
  for (const subpath of [0, false, {}, []]) {
    const mounts = [
      { volumeId: "shared", mountPath: "/valid" },
      { volumeId: "native", mountPath: "/invalid", subpath },
    ];
    await assert.rejects(service.resolveMounts(mounts), TypeError);
    await assert.rejects(service.assertNativeMounts(mounts), TypeError);
  }
  assert.equal(arkerLists, 0);
  assert.equal(native.calls.length, 0);
});

test("attachment waits as the Daytona guest and cleans up a failed new VM", async () => {
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => nativeService().service as never);
  const calls: unknown[] = [];
  const vm = { async createMount(value: unknown) { calls.push(value); } };
  const box = {
    provider: "arker", sandboxId: "vm-one", filesystem: {}, getInstance: () => vm,
    async runCommand(command: string) { calls.push(command); return { stdout: "", stderr: "", exitCode: 1, durationMs: 0 }; },
    async destroy() { calls.push("destroy"); },
  };
  await assert.rejects(service.attach(box as never, [{ filesystemId: "dir_one", mountPath: "/mnt/shared" }]), /did not become accessible/);
  assert.deepEqual(calls[0], { filesystemId: "dir_one", mountPath: "/mnt/shared" });
  assert.match(calls[1] as string, /setpriv --reuid=daytona/);
  assert.match(calls[1] as string, /mountinfo/);
  assert.equal(calls[2], "destroy");
});

test("attachment cleanup failure retains both the primary and cleanup errors", async () => {
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => nativeService().service as never);
  const primary = new Error("readiness failed");
  const cleanup = new Error("cleanup failed");
  const box = {
    provider: "arker", sandboxId: "vm-both", filesystem: {}, getInstance: () => ({ async createMount() { throw primary; } }),
    async destroy() { throw cleanup; },
  };
  await assert.rejects(service.attach(box as never, [{ filesystemId: "dir_one", mountPath: "/mnt/shared" }]), (error: unknown) => {
    assert.ok(error instanceof AggregateError);
    assert.match(error.message, /vm-both/);
    assert.deepEqual(error.errors, [primary, cleanup]);
    return true;
  });
});

test("Arker create conflicts use Daytona's native bad-request contract", async (t) => {
  patchArker(t);
  Arker.prototype.createFilesystem = async () => { throw new ArkerError("already_exists", "duplicate", 409); };
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => nativeService().service as never);
  await assert.rejects(service.create("duplicate"), (error: any) => error?.statusCode === 400);
});

test("Arker authorization failures use Daytona's exact forbidden class", async (t) => {
  patchArker(t);
  Arker.prototype.listFilesystems = async () => { throw new ArkerError("forbidden", "denied", 403); };
  const service = new DaytonaVolumeService({ apiKey: "arker-test" }, () => nativeService().service as never);
  await assert.rejects(service.get("shared"), DaytonaForbiddenError);
});

test("native create and get defer empty-name validation to Daytona", async (t) => {
  preserveEnvironment(t, "ARKER_API_KEY", "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.ARKER_API_KEY;
  const native = nativeService();
  const service = new DaytonaVolumeService(undefined, () => native.service as never);
  assert.equal((await service.create("")).name, "");
  await assert.rejects(service.get(""), DaytonaNotFoundError);
  assert.deepEqual(native.calls.slice(0, 2), [["create", ""], ["get", ""]]);
});
