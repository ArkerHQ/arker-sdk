import assert from "node:assert/strict";
import { test } from "node:test";

import { Arker } from "../src/index.js";
import { FakeFetch } from "./helpers/scripted-fetch.js";

function client(fetch: FakeFetch): Arker {
  return new Arker({
    apiKey: "ark_test",
    baseUrl: "https://test.invalid/api/",
    fetch: fetch.fetch,
    retry: false,
  });
}

test("conditional filesystem deletion uses the atomic query contract", async () => {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "DELETE" && url === "https://test.invalid/api/v1/filesystems/fs_1?only_if_unmounted=true",
    200,
    { deleted: true },
  );
  assert.equal((await client(fetch).deleteFilesystem("fs_1", { onlyIfUnmounted: true })).deleted, true);
});

test("mount creation sends and returns the filesystem subpath", async () => {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/mounts",
    200,
    { mount_id: "mount_1", vm_id: "vm_1", filesystem_id: "fs_1", path: "/data", subpath: "nested/data" },
  );
  const mount = await client(fetch).vm("vm_1").createMount({
    filesystemId: "fs_1",
    path: "/data",
    subpath: "nested/data",
  });
  assert.equal(mount.subpath, "nested/data");
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), {
    filesystem_id: "fs_1",
    path: "/data",
    subpath: "nested/data",
  });
});

test("empty mount subpaths are omitted from the wire request", async () => {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/mounts",
    200,
    { mount_id: "mount_1", vm_id: "vm_1", filesystem_id: "fs_1", path: "/data" },
  );
  await client(fetch).vm("vm_1").createMount({ filesystemId: "fs_1", path: "/data", subpath: "" });
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), { filesystem_id: "fs_1", path: "/data" });
});
