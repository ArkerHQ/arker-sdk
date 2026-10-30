import assert from "node:assert/strict";
import { test } from "node:test";
import { blaxelDrives } from "../src/compat/blaxel-drives.js";

for (const sameBinding of [true, false]) {
  test(`mount conflict ${sameBinding ? "reconciles the identical binding" : "preserves a different binding"}`, async () => {
    const file = {
      filesystem_id: "dir-test",
      name: "shared",
      region: "region-one",
    };
    const conflict = Object.assign(new Error("mount exists"), { status: 409 });
    let reads = 0;
    let creates = 0;
    const vm = {
      provider: "aws",
      region: "region-one",
      async listMounts() {
        reads++;
        return {
          mounts:
            reads === 1
              ? []
              : [
                  {
                    mount_id: "mount-test",
                    path: "/data",
                    filesystem_id: sameBinding
                      ? file.filesystem_id
                      : "dir-other",
                    state: "mounted",
                  },
                ],
        };
      },
      async createMount() {
        creates++;
        throw conflict;
      },
      async deleteMount() {
        assert.fail("unexpected deletion");
      },
    } as unknown as Parameters<typeof blaxelDrives>[0];
    const filesystems = {
      provider: "aws",
      region: "region-one",
      async listFilesystems() {
        return { filesystems: [file] };
      },
      async getFilesystem() {
        return file;
      },
    } as unknown as Parameters<typeof blaxelDrives>[1];
    const result = blaxelDrives(vm, filesystems).mount({
      driveName: "shared",
      mountPath: "/data",
    });
    if (sameBinding)
      assert.deepEqual(await result, {
        driveName: "shared",
        mountPath: "/data",
        drivePath: "/",
        readOnly: false,
        success: true,
      });
    else await assert.rejects(result, (error) => error === conflict);
    assert.equal(creates, 1, "a conflict must never replay the POST");
  });
}

test("concurrent identical mounts both settle against one binding", async () => {
  let release!: () => void;
  const barrier = new Promise<void>((resolve) => {
    release = resolve;
  });
  let reads = 0,
    posts = 0;
  const rows: Array<{
    filesystem_id: string;
    mount_id: string;
    path: string;
    state: string;
  }> = [];
  const file = {
    filesystem_id: "dir-test",
    name: "shared",
    region: "region-one",
  };
  const vm = {
    provider: "aws",
    region: "region-one",
    async listMounts() {
      if (++reads <= 2) {
        if (reads === 2) release();
        await barrier;
        return { mounts: [] };
      }
      return { mounts: rows };
    },
    async createMount() {
      posts++;
      if (rows.length)
        throw Object.assign(new Error("exists"), { status: 409 });
      rows.push({
        filesystem_id: file.filesystem_id,
        mount_id: "mount-test",
        path: "/data",
        state: "mounted",
      });
      return rows[0];
    },
  } as unknown as Parameters<typeof blaxelDrives>[0];
  const fs = {
    provider: "aws",
    region: "region-one",
    async listFilesystems() {
      return { filesystems: [file] };
    },
    async getFilesystem() {
      return file;
    },
  } as unknown as Parameters<typeof blaxelDrives>[1];
  const drives = blaxelDrives(vm, fs);
  const results = await Promise.all([
    drives.mount({ driveName: "shared", mountPath: "/data" }),
    drives.mount({ driveName: "shared", mountPath: "/data" }),
  ]);
  assert.deepEqual(results[0], results[1]);
  assert.equal(rows.length, 1);
  assert.equal(posts, 2);
});

test("mount transport does not replay a POST whose response was lost", async () => {
  const { Arker, VM } = await import("../src/index.js");
  let calls = 0;
  const client = new Arker({
    apiKey: "test",
    baseUrl: "https://custom.example/api",
    fetch: async (_input, init) => {
      assert.equal(init?.method, "POST");
      calls++;
      throw new TypeError("connection lost after request acceptance");
    },
  });
  const vm = new VM(client, "vm-test", client.baseUrl);
  await assert.rejects(
    vm.createMount({ filesystemId: "dir-test", path: "/data" }),
  );
  assert.equal(calls, 1);
});
