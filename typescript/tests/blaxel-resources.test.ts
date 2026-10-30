import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DriveInstance as NativeDrive, VolumeInstance as NativeVolume, createPaginatedList } from "@blaxel/core";
import { DriveInstance, VolumeInstance } from "../src/blaxel.js";
import { Arker, ArkerError } from "../src/index.js";
import { preserveEnvironment, preserveProperties } from "./provider-fixtures.js";

async function setup(t: TestContext, order = ["arker"]) {
  preserveEnvironment(t, "ARKER_API_KEY", "ARKER_BASE_URL", "COMPUTESDK_ROUTER_CONFIG");
  process.env.ARKER_API_KEY = "test-key";
  process.env.ARKER_BASE_URL = "https://filesystem.example/api";
  const dir = await mkdtemp(join(tmpdir(), "blaxel-resources-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const path = join(dir, "router.json");
  await writeFile(path, JSON.stringify({ version: 1, existing: { strategy: "priority", order }, create: { strategy: "priority", order } }));
  process.env.COMPUTESDK_ROUTER_CONFIG = path;
  preserveProperties(t, Arker.prototype, "_request");
  preserveProperties(t, NativeDrive, "create", "get", "list", "delete", "update");
  preserveProperties(t, NativeVolume, "create", "get", "list", "delete");
  NativeDrive.create = async () => { throw new Error("native mutation forbidden"); };
  NativeDrive.delete = async () => { throw new Error("native mutation forbidden"); };
  NativeDrive.update = async () => { throw new Error("native mutation forbidden"); };
  NativeDrive.get = async () => { throw { code: 404 }; };
  const file = { filesystem_id: "dir-one", name: "data", region: "test-region", provider: "aws", owner_org_id: "org", created_at: "2026-01-01T00:00:00Z" };
  const calls: Array<{ method: string, path: string, body: unknown, retry: unknown ;}> = [];
  Arker.prototype._request = async function(method, path, body, _base, _headers, _signal, retry) {
    calls.push({ method, path, body, retry });
    if (method === "POST") return file as never;
    if (method === "DELETE") return { deleted: true } as never;
    if (path.startsWith("/v1/filesystems?")) return { filesystems: [file], next_cursor: null } as never;
    if (path === "/v1/filesystems/dir-one") return file as never;
    throw new Error(`Unexpected request ${method} ${path}`);
  };
  return { file, calls };
}

test("one-import drive create binds native model to conditional Arker delete and refuses update", async t => {
  const { calls } = await setup(t);
  const drive = await DriveInstance.create({ name: "data" });
  assert.ok(drive instanceof NativeDrive);
  assert.equal(drive.name, "data");
  assert.equal(drive.region, "test-region");
  for (const value of [drive.size, drive.state, drive.status, drive.permissions]) assert.equal(value, undefined);
  await assert.rejects(drive.update({ name: "changed" }), /unsupported/i);
  await drive.delete();
  const deletion = calls.find(c => c.method === "DELETE")!;
  assert.match(deletion.path, /only_if_unmounted=true/);
  assert.equal(deletion.retry, false);
  assert.equal(calls.filter(c => c.method === "POST").length, 1);
});

test("exact name lookup traverses pages and never confuses a prefix", async t => {
  const { file, calls } = await setup(t);
  Arker.prototype._request = async function(method, path) {
    calls.push({ method, path, body: undefined, retry: undefined });
    if (path.includes("cursor=next")) return { filesystems: [file], next_cursor: null } as never;
    if (path.includes("?")) return { filesystems: [{ ...file, name: "data-old", filesystem_id: "other" }], next_cursor: "next" } as never;
    return file as never;
  };
  assert.equal((await DriveInstance.get("data")).name, "data");
  assert.ok(calls.some(c => c.path.includes("cursor=next")));
  assert.ok(calls.some(c => c.path === "/v1/filesystems/dir-one"));
});

test("ambiguous cross-provider static delete refuses before mutation", async t => {
  const { calls } = await setup(t, ["arker", "blaxel"]);
  NativeDrive.get = async () => new NativeDrive({ metadata: { name: "data" }, spec: {} });
  await assert.rejects(DriveInstance.delete("data"), /ambiguous/);
  assert.ok(calls.every(c => c.method === "GET"));
});

test("native-only calls preserve options, result identity and native pagination", async t => {
  await setup(t, ["blaxel"]);
  const options = { name: "data", region: "native-region" };
  const native = new NativeDrive({ metadata: { name: "data" }, spec: {} });
  NativeDrive.create = async value => { assert.equal(value, options); return native; };
  assert.equal(await DriveInstance.create(options), native);
  const query = { limit: 2, sort: "name:desc" as const };
  const page = await createPaginatedList({ response: [native], fetchPage: async () => [], mapItem: x => x });
  NativeDrive.list = async value => { assert.equal(value, query); return page; };
  assert.equal(await DriveInstance.list(query), page);
  const volume = new NativeVolume({ metadata: { name: "volume" }, spec: { size: 1024 } });
  NativeVolume.create = async value => { assert.deepEqual(value, { name: "volume" }); return volume; };
  assert.equal(await VolumeInstance.create({ name: "volume" }), volume);
});

test("unsupported drive fields and implicit volume capacity refuse before mutation", async t => {
  const { calls } = await setup(t);
  for (const options of [{ name: "data", permissions: [] }, { name: "data", region: "vendor" }, { name: "data", labels: {} }, { metadata: { name: "data" }, spec: { size: 1 } }]) {
    await assert.rejects(DriveInstance.create(options as never), /unsupported/i);
  }
  await assert.rejects(VolumeInstance.create({ name: "volume" }), /capacity|size/i);
  assert.equal(calls.length, 0);
});

test("accepted creation with lost response is neither replayed nor sent to native", async t => {
  await setup(t, ["arker", "blaxel"]);
  let requests = 0;
  const lost = new ArkerError("unavailable", "lost response", 503);
  Arker.prototype._request = async () => { requests++; throw lost; };
  await assert.rejects(DriveInstance.create({ name: "data" }), e => e === lost);
  assert.equal(requests, 1);
});

test("mixed pages retain provider order, skip empty providers, and reject wrong cursor kind", async t => {
  const { file, calls } = await setup(t, ["blaxel", "arker"]);
  let nativeCalls = 0;
  NativeDrive.list = async query => {
    nativeCalls++;
    assert.equal(query?.limit, 1);
    const value = new NativeDrive({ metadata: { name: "native" }, spec: {} });
    return createPaginatedList({ response: [value], fetchPage: async () => [], mapItem: x => x });
  };
  const first = await DriveInstance.list({ limit: 1 });
  assert.equal(first.data[0]!.name, "native");
  assert.equal(calls.length, 0);
  const cursor = first.nextCursor!;
  assert.deepEqual(JSON.parse(Buffer.from(cursor, "base64url").toString()), ["drive", ["blaxel", "arker"], 1, null]);
  const next = await first.nextPage();
  assert.equal(next!.data[0]!.name, file.name);
  assert.equal(next!.hasMore, false);
  assert.equal(nativeCalls, 1);
  await assert.rejects(VolumeInstance.list({ cursor }), /Cursor/);
  NativeDrive.list = async () => createPaginatedList({ response: [], fetchPage: async () => [], mapItem: x => x as NativeDrive });
  assert.equal((await DriveInstance.list()).data[0]!.name, file.name);
});

test("Arker-only pages expose raw backend cursor and use native paginator helpers", async t => {
  const { file } = await setup(t);
  const cursors: string[] = [];
  Arker.prototype._request = async function(_method, path) {
    cursors.push(path);
    return { filesystems: [file], next_cursor: path.includes("cursor=next") ? null : "next" } as never;
  };
  const first = await DriveInstance.list({ limit: 1 });
  assert.equal(first.nextCursor, "next");
  const rows = await first.autoPagingToArray({ limit: 2 });
  assert.equal(rows.length, 2);
  assert.equal(cursors.length, 2);
  assert.ok(cursors[1]!.includes("cursor=next"));
});

test("native-only error object and delete response are returned unchanged without a get probe", async t => {
  await setup(t, ["blaxel"]);
  const failure = { code: 404, error: "native exact message" };
  NativeDrive.get = async () => { throw failure; };
  await assert.rejects(DriveInstance.get("missing"), error => error === failure);
  const response = { name: "data", message: "removed" };
  NativeDrive.delete = async () => response;
  assert.equal(await DriveInstance.delete("data"), response);
});

test("same-provider confirmed conflict can resolve createIfNotExists, auth cannot fall through", async t => {
  const { file } = await setup(t, ["arker", "blaxel"]);
  let posts = 0;
  Arker.prototype._request = async function(method, path) {
    if (method === "POST") { posts++; throw new ArkerError("already_exists", "exists", 409); }
    return (path.includes("?") ? { filesystems: [file], next_cursor: null } : file) as never;
  };
  assert.equal((await DriveInstance.createIfNotExists({ name: "data" })).name, "data");
  assert.equal(posts, 1);
  const denied = new ArkerError("forbidden", "denied", 403);
  Arker.prototype._request = async () => { throw denied; };
  await assert.rejects(DriveInstance.get("data"), error => error === denied);
});

test("refused mounted deletion preserves the backend error and never retries or forces", async t => {
  const { file } = await setup(t);
  const refused = new ArkerError("resource_busy", "mounted", 409);
  let deletes = 0;
  Arker.prototype._request = async function(method, path, _body, _base, _headers, _signal, retry) {
    if (method === "POST") { assert.equal(retry, false); return file as never; }
    assert.equal(method, "DELETE");
    assert.match(path, /only_if_unmounted=true/);
    assert.equal(retry, false);
    deletes++; throw refused;
  };
  const drive = await DriveInstance.create({ name: "data" });
  await assert.rejects(drive.delete(), error => error === refused);
  assert.equal(deletes, 1);
});

test("repeated backend cursor refuses incomplete name lookup", async t => {
  const { file } = await setup(t);
  let reads = 0;
  Arker.prototype._request = async () => { reads++; return { filesystems: [{ ...file, name: "other" }], next_cursor: "repeat" } as never; };
  await assert.rejects(DriveInstance.get("missing"), /repeated/);
  assert.equal(reads, 2);
});

test("volume readback shares the filesystem namespace without invented capacity or update fallback", async t => {
  await setup(t);
  const volume = await VolumeInstance.get("data");
  assert.ok(volume instanceof NativeVolume);
  assert.equal(volume.size, undefined);
  assert.equal(volume.status, undefined);
  await assert.rejects(volume.update({ size: 2048 }), /Unsupported/);
  const page = await VolumeInstance.list();
  assert.equal(page.data[0]!.name, "data");
});

test("mixed cursor and list limit have no invented provider-independent upper bound", async t => {
  const { file } = await setup(t, ["arker", "blaxel"]);
  const backend = "c".repeat(17000);
  const cursor = Buffer.from(JSON.stringify(["drive", ["arker", "blaxel"], 0, backend])).toString("base64url");
  Arker.prototype._request = async function(_method, path) {
    const query = new URL(path, "https://filesystem.example").searchParams;
    assert.equal(query.get("cursor"), backend);
    assert.equal(query.get("limit"), "1001");
    return { filesystems: [file], next_cursor: null } as never;
  };
  assert.equal((await DriveInstance.list({ cursor, limit: 1001 })).data[0]!.name, "data");
});

test("static delete refuses an ownership probe failure even when another provider has the name", async t => {
  const { calls } = await setup(t, ["arker", "blaxel"]);
  const denied = { code: 403, error: "native denied" };
  NativeDrive.get = async () => { throw denied; };
  await assert.rejects(DriveInstance.delete("data"), error => error === denied);
  assert.ok(calls.every(call => call.method === "GET"));
});

for (const Resource of [DriveInstance, VolumeInstance]) {
  test(`${Resource.name} rejects changed authoritative name before deletion`, async t => {
    const { file, calls } = await setup(t);
    Arker.prototype._request = async function(method, path) {
      calls.push({ method, path, body: undefined, retry: undefined });
      if (path.includes("?")) return { filesystems: [file], next_cursor: null } as never;
      return { ...file, name: "renamed" } as never;
    };
    await assert.rejects(Resource.get("data"), /identity changed/i);
    await assert.rejects(Resource.delete("data"), /identity changed/i);
    assert.ok(calls.every(call => call.method === "GET"));
  });
}
