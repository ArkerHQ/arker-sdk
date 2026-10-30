import assert from "node:assert/strict";
import { test } from "node:test";
import { SandboxDriveSetupError, type SandboxInstance } from "@blaxel/core";
import {
  prepareBlaxelDrives,
  blaxelDrives,
} from "../src/compat/blaxel-drives.js";

type File = {
  filesystem_id: string;
  name: string;
  region: string;
  provider: string;
};
type Row = {
  mount_id: string;
  filesystem_id: string;
  path: string;
  state: string;
};
function fixture() {
  const files: File[] = [],
    rows: Row[] = [],
    deleted: string[] = [];
  const sandbox = { metadata: { name: "test" } } as SandboxInstance;
  const vm = {
    provider: "aws",
    region: "region-one",
    async listMounts() {
      return { mounts: rows };
    },
    async createMount(body: { filesystemId: string; path: string }) {
      const row = {
        mount_id: "mount-test",
        filesystem_id: body.filesystemId,
        path: body.path,
        state: "mounted",
      };
      rows.push(row);
      return row;
    },
    async deleteMount() {
      return { deleted: true };
    },
  };
  const fs = {
    provider: "aws",
    region: "region-one",
    async listFilesystems() {
      return { filesystems: files };
    },
    async getFilesystem(id: string) {
      return files.find((f) => f.filesystem_id === id)!;
    },
    async createFilesystem(body: { name: string }) {
      const file = {
        filesystem_id: `dir-${files.length}`,
        name: body.name,
        region: vm.region,
        provider: vm.provider,
      };
      files.push(file);
      return file;
    },
    async deleteFilesystem(id: string, options: unknown) {
      assert.deepEqual(options, { onlyIfUnmounted: true });
      deleted.push(id);
      return { deleted: true };
    },
  };
  return {
    vm,
    fs,
    files,
    rows,
    deleted,
    sandbox,
    mount(values: unknown) {
      return prepareBlaxelDrives(values).mount(
        sandbox,
        vm as unknown as Parameters<typeof blaxelDrives>[0],
        fs as unknown as Parameters<typeof blaxelDrives>[1],
      );
    },
  };
}
const refused = Object.assign(new Error("refused"), { status: 422 });
for (const chosen of [true, false])
  test(`failed mount retains ${chosen ? "chosen" : "cleans generated"} filesystem`, async () => {
    const f = fixture();
    f.vm.createMount = async () => {
      throw refused;
    };
    await assert.rejects(
      f.mount([
        { create: chosen ? { name: "shared" } : {}, mountPath: "/data" },
      ]),
      (error) => {
        assert(error instanceof SandboxDriveSetupError);
        assert.equal(error.sandbox, f.sandbox);
        assert.equal(error.cause, refused);
        assert.equal(error.createdDrives.length, chosen ? 1 : 0);
        return true;
      },
    );
    assert.equal(f.deleted.length, chosen ? 0 : 1);
  });
test("cleanup failure preserves primary, cleanup error and retained filesystem", async () => {
  const f = fixture(),
    cleanup = new Error("cleanup failed");
  f.vm.createMount = async () => {
    throw refused;
  };
  f.fs.deleteFilesystem = async () => {
    throw cleanup;
  };
  await assert.rejects(
    f.mount([{ create: {}, mountPath: "/data" }]),
    (error) => {
      assert(error instanceof SandboxDriveSetupError);
      assert.equal(error.cause, refused);
      assert.deepEqual(Reflect.get(error, "cleanupErrors"), [cleanup]);
      assert.equal(error.createdDrives.length, 1);
      return true;
    },
  );
});
test("lost create response never authorizes deletion or replay", async () => {
  const f = fixture(),
    lost = new Error("response lost");
  let creates = 0;
  const create = f.fs.createFilesystem;
  f.fs.createFilesystem = async (body) => {
    creates++;
    await create(body);
    throw lost;
  };
  await assert.rejects(
    f.mount([{ create: {}, mountPath: "/data" }]),
    (error) => {
      assert(error instanceof SandboxDriveSetupError);
      assert.equal(error.cause, lost);
      assert.equal(error.createdDrives.length, 1);
      return true;
    },
  );
  assert.equal(creates, 1);
  assert.deepEqual(f.deleted, []);
  assert.deepEqual(f.rows, []);
});
test("named filesystem conflict reuses existing resource without cleanup ownership", async () => {
  const f = fixture();
  await f.fs.createFilesystem({ name: "shared" });
  f.fs.createFilesystem = async () => {
    throw Object.assign(new Error("exists"), { status: 409 });
  };
  await f.mount([{ create: { name: "shared" }, mountPath: "/data" }]);
  assert.equal(f.rows[0]?.filesystem_id, f.files[0]?.filesystem_id);
  assert.deepEqual(f.deleted, []);
});
test("partial setup preserves successful mount and removes only unused generated drive", async () => {
  const f = fixture(),
    mount = f.vm.createMount;
  f.vm.createMount = async (body) => {
    if (body.path === "/second") throw refused;
    return mount(body);
  };
  await assert.rejects(
    f.mount([
      { create: {}, mountPath: "/first" },
      { create: {}, mountPath: "/second" },
    ]),
    SandboxDriveSetupError,
  );
  assert.deepEqual(f.deleted, ["dir-1"]);
  assert.equal(f.rows.length, 1);
});
test("uncertain mount outcome with unavailable readback retains filesystem", async () => {
  const f = fixture();
  let failed = false;
  f.vm.createMount = async () => {
    failed = true;
    throw new Error("response lost");
  };
  f.vm.listMounts = async () => {
    if (failed) throw new Error("read unavailable");
    return { mounts: [] };
  };
  await assert.rejects(
    f.mount([{ create: {}, mountPath: "/data" }]),
    SandboxDriveSetupError,
  );
  assert.deepEqual(f.deleted, []);
});
test("lost mount response reconciles without replay", async () => {
  const f = fixture(),
    mount = f.vm.createMount;
  let creates = 0;
  f.vm.createMount = async (body) => {
    creates++;
    await mount(body);
    throw new Error("response lost");
  };
  await f.mount([{ create: {}, mountPath: "/data" }]);
  assert.equal(creates, 1);
  assert.deepEqual(f.deleted, []);
});
for (const value of [
  { driveName: "shared", mountPath: "/data", readOnly: true },
  { create: { permissions: {} }, mountPath: "/data" },
  { create: { labels: {} }, mountPath: "/data" },
  { driveName: "shared", mountPath: "/" },
  { driveName: "shared", mountPath: "/data/../other" },
])
  test(`unsupported setup fails before mutation: ${JSON.stringify(value)}`, () => {
    assert.throws(() => prepareBlaxelDrives([value]), TypeError);
  });
test("atomic create requires named drives before mutation", () => {
  assert.throws(
    () =>
      prepareBlaxelDrives([{ create: {}, mountPath: "/data" }], {
        createIfNotExist: true,
      }),
    TypeError,
  );
});
test("two named setup calls share the authoritative filesystem", async () => {
  const f = fixture(),
    create = f.fs.createFilesystem;
  let creates = 0;
  f.fs.createFilesystem = async (body) => {
    creates++;
    if (f.files.some((file) => file.name === body.name))
      throw Object.assign(new Error("exists"), { status: 409 });
    return create(body);
  };
  await Promise.all([
    f.mount([{ create: { name: "shared" }, mountPath: "/one" }]),
    f.mount([{ create: { name: "shared" }, mountPath: "/two" }]),
  ]);
  assert.equal(creates, 2);
  assert.equal(f.files.length, 1);
  assert.equal(f.rows.length, 2);
  assert.deepEqual(f.deleted, []);
});
test("generated-name conflict never borrows or deletes the conflicting resource", async () => {
  const f = fixture(),
    conflict = Object.assign(new Error("exists"), { status: 409 });
  f.fs.createFilesystem = async () => {
    throw conflict;
  };
  await assert.rejects(
    f.mount([{ create: {}, mountPath: "/data" }]),
    (error) => {
      assert(error instanceof SandboxDriveSetupError);
      assert.equal(error.cause, conflict);
      assert.deepEqual(error.createdDrives, []);
      return true;
    },
  );
  assert.deepEqual(f.deleted, []);
  assert.deepEqual(f.rows, []);
});
test("an aborted setup performs no mutation and retains native setup error", async () => {
  const f = fixture(),
    controller = new AbortController();
  controller.abort(new Error("cancelled"));
  await assert.rejects(
    prepareBlaxelDrives([{ create: {}, mountPath: "/data" }]).mount(
      f.sandbox,
      f.vm as unknown as Parameters<typeof blaxelDrives>[0],
      f.fs as unknown as Parameters<typeof blaxelDrives>[1],
      { signal: controller.signal },
    ),
    (error) => {
      assert(error instanceof SandboxDriveSetupError);
      assert.equal(error.cause, controller.signal.reason);
      return true;
    },
  );
  assert.deepEqual(f.files, []);
  assert.deepEqual(f.rows, []);
});
test("name lookup scans pages and ignores prefix matches", async () => {
  const f = fixture();
  const exact = await f.fs.createFilesystem({ name: "shared" });
  const queries: Array<string | undefined> = [];
  f.fs.listFilesystems = async (options: { cursor?: string } = {}) => {
    queries.push(options.cursor);
    return options.cursor
      ? { filesystems: [exact] }
      : {
          filesystems: [
            { ...exact, filesystem_id: "dir-prefix", name: "shared-extra" },
          ],
          next_cursor: "next",
        };
  };
  await f.mount([{ driveName: "shared", mountPath: "/data" }]);
  assert.deepEqual(queries, [undefined, "next"]);
  assert.equal(f.rows[0]?.filesystem_id, exact.filesystem_id);
});
test("failed readiness preserves the mount and its filesystem", async () => {
  const f = fixture(),
    mount = f.vm.createMount;
  f.vm.createMount = async (body) => {
    const row = await mount(body);
    row.state = "failed";
    return row;
  };
  await assert.rejects(
    f.mount([{ create: {}, mountPath: "/data" }]),
    (error) => {
      assert(error instanceof SandboxDriveSetupError);
      assert.match(String(error.cause), /mount failed/);
      return true;
    },
  );
  assert.deepEqual(f.deleted, []);
  assert.equal(f.rows.length, 1);
});
