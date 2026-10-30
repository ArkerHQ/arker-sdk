import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  SandboxInstance as Native,
  SandboxDriveSetupError,
} from "@blaxel/core";
import { Arker, ArkerError } from "../src/index.js";
import { SandboxInstance } from "../src/blaxel.js";
import { createBlaxelDataPlane } from "../src/compat/blaxel-data-plane.js";
import { preserveProperties } from "./provider-fixtures.js";

for (const failMount of [false, true])
  test(`drive setup ${failMount ? "preserves ready sandbox on failure" : "binds native component and reconnects"}`, async (t) => {
    const dir = await mkdtemp(join(tmpdir(), "blaxel-drive-facade-"));
    t.after(() => rm(dir, { recursive: true, force: true }));
    const routerConfigPath = join(dir, "router.json");
    await writeFile(
      routerConfigPath,
      JSON.stringify({
        version: 1,
        create: { strategy: "priority", order: ["arker"] },
        existing: { strategy: "priority", order: ["arker"] },
      }),
    );
    const planes: ReturnType<typeof createBlaxelDataPlane>[] = [];
    t.after(() => planes.forEach((p) => p.close()));
    const originalWrap = Reflect.get(SandboxInstance, "wrap");
    t.after(() => Reflect.set(SandboxInstance, "wrap", originalWrap));
    Reflect.set(
      SandboxInstance,
      "wrap",
      async (box: unknown, profile: unknown, config: unknown) => {
        const plane = createBlaxelDataPlane({
          connectPort: async () => {
            throw new Error("unexpected daemon I/O");
          },
        });
        planes.push(plane);
        return Reflect.construct(SandboxInstance, [
          box,
          undefined,
          profile,
          plane,
          config,
        ]);
      },
    );
    preserveProperties(t, Arker.prototype, "_request");
    const file = {
      filesystem_id: "dir-test",
      name: "drive-test",
      region: "region-one",
      provider: "aws",
      owner_org_id: "org",
      created_at: "2026-01-01T00:00:00Z",
    };
    const vm = {
      vm_id: "vm-test",
      name: "named",
      provider: "aws",
      region: "region-one",
      description: null,
      owner_org_id: "org",
      created_at: "2026-01-01T00:00:00Z",
      public: false,
      state: "idle",
      sessions: [],
      network: {},
      resources: {},
    };
    let mounted = false;
    const calls: Array<{ method: string; path: string; base: unknown }> = [];
    const mount = {
      mount_id: "mount-test",
      vm_id: vm.vm_id,
      filesystem_id: file.filesystem_id,
      path: "/data",
      subpath: "nested",
      state: "mounted",
      region: vm.region,
    };
    Arker.prototype._request = async function (method, path, body, base) {
      calls.push({ method, path, base });
      if (path.startsWith("/v1/filesystems")) {
        assert.equal(base, "https://regional.custom.example/api");
        if (method === "GET")
          return (
            path.split("?")[0] === "/v1/filesystems"
              ? { filesystems: [file], next_cursor: null }
              : file
          ) as never;
        assert.fail("borrowed drive mutated");
      }
      if (path.endsWith("/mounts")) {
        if (method === "POST") {
          assert.deepEqual(body, {
            filesystem_id: file.filesystem_id,
            path: "/data",
            subpath: "nested",
          });
          if (failMount)
            throw new ArkerError("invalid_state", "mount refused", 409);
          mounted = true;
          return mount as never;
        }
        return { mounts: mounted ? [mount] : [], next_cursor: null } as never;
      }
      if (method === "DELETE")
        assert.fail("ready sandbox deleted after mount failure");
      return vm as never;
    };
    const config = {
      routerConfigPath,
      arker: {
        apiKey: "ark_test",
        source: "source",
        baseUrl: "https://regional.custom.example/api",
        controlBaseUrl: "https://global.custom.example/api",
      },
    };
    const create = () =>
      SandboxInstance.create(
        { name: "named" },
        {
          ...config,
          mountDrives: [
            { driveName: file.name, mountPath: "/data", drivePath: "/nested" },
          ],
        },
      );
    if (failMount) {
      await assert.rejects(create(), (error) => {
        assert(error instanceof SandboxDriveSetupError);
        assert(error.sandbox instanceof SandboxInstance);
        assert.deepEqual(error.driveNames, [file.name]);
        assert.deepEqual(error.createdDrives, []);
        return true;
      });
    } else {
      const box = await create();
      const native = new Native({
        metadata: { name: "test" },
        spec: {},
        forceUrl: "http://localhost:49983",
      });
      assert.equal(box.drives.constructor, native.drives.constructor);
      assert.deepEqual(await box.drives.list(), [
        {
          driveName: file.name,
          mountPath: "/data",
          drivePath: "/nested",
          readOnly: false,
        },
      ]);
      const reconnected = await SandboxInstance.get("named", config);
      assert.deepEqual(
        await reconnected.drives.list(),
        await box.drives.list(),
      );
    }
    assert.equal(
      calls.filter((c) => c.method === "POST" && c.path.endsWith("/mounts"))
        .length,
      1,
    );
  });
