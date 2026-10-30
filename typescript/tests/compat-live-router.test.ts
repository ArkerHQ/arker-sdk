import assert from "node:assert/strict";
import filesystem, { access, mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { syncBuiltinESMExports } from "node:module";
import { test } from "node:test";
import { withLiveRouter } from "./compat-live-router.js";

const credential = "COMPAT_LIVE_ROUTER_REGRESSION_KEY";
const router = "COMPUTESDK_ROUTER_CONFIG";

test("live router lifecycle (no provider imports or live credentials)", async (t) => {
  const saved = [credential, router].map((key) => [key, process.env[key]] as const);
  try {
    await t.test("missing, empty and blank credentials fail before the callback", async () => {
      for (const value of [undefined, "", " \t"]) {
        if (value === undefined) delete process.env[credential];
        else process.env[credential] = value;
        await assert.rejects(withLiveRouter([credential], async () => {
          assert.fail("must not start a scenario without credentials");
        }), new RegExp(`Missing live credentials/config: ${credential}`));
      }
    });
    process.env[credential] = "test-only";
    for (const previous of [undefined, "", "previous-router"]) {
      await t.test(`restore router ${JSON.stringify(previous)} and credentials after success`, async () => {
        if (previous === undefined) delete process.env[router];
        else process.env[router] = previous;
        let temporary = "";
        await withLiveRouter([credential], async ({ directory, setProvider }) => {
          temporary = directory;
          // The three explicit differential legs select native, native, then Arker.
          for (const provider of ["e2b", "e2b", "arker"] as const) {
            await setProvider(provider);
            assert.deepEqual(JSON.parse(await readFile(process.env[router]!, "utf8")), {
              version: 1,
              existing: { strategy: "priority", order: [provider] },
              create: { strategy: "priority", order: [provider] },
            });
          }
          await writeFile(join(directory, "fixture"), "temporary data");
          delete process.env[credential];
        });
        assert.equal(process.env[router], previous);
        assert.equal(process.env[credential], "test-only");
        await assert.rejects(access(temporary), { code: "ENOENT" });
      });
    }
    for (const stage of ["setup", "callback"]) {
      await t.test(`restore environment and remove files after ${stage} failure`, async () => {
        const failure = new Error(stage);
        let temporary = "";
        await assert.rejects(withLiveRouter([credential], async ({ directory, setProvider }) => {
          temporary = directory;
          process.env[credential] = "changed";
          if (stage === "setup") await mkdir(join(directory, "router.json"));
          await setProvider("arker");
          throw failure;
        }), stage === "setup" ? { code: "EISDIR" } : (error: unknown) => error === failure);
        assert.equal(process.env[router], "previous-router");
        assert.equal(process.env[credential], "test-only");
        await assert.rejects(access(temporary), { code: "ENOENT" });
      });
    }
    await t.test("temporary cleanup failure preserves the callback error identity", async (t) => {
      const original = new Error("scenario callback failed");
      let temporary = "";
      const cleanup = t.mock.method(filesystem, "rm", async () => {
        throw new Error("temporary cleanup failed");
      });
      const diagnostic = t.mock.method(console, "error", () => {});
      syncBuiltinESMExports();
      try {
        await assert.rejects(withLiveRouter([credential], async ({ directory, setProvider }) => {
          temporary = directory;
          await setProvider("blaxel");
          delete process.env[credential];
          throw original;
        }), (error: unknown) => error === original);
        assert.equal(cleanup.mock.callCount(), 1);
        assert.equal(diagnostic.mock.callCount(), 1);
        assert.equal(process.env[router], "previous-router");
        assert.equal(process.env[credential], "test-only");
      } finally {
        cleanup.mock.restore();
        diagnostic.mock.restore();
        syncBuiltinESMExports();
        if (temporary) await filesystem.rm(temporary, { recursive: true, force: true });
      }
    });
  } finally {
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
