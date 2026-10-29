import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { stageEnvdArtifact } from "../src/compat/e2b-artifact.js";

test("verified offline cache stages with bounded sync, rejects corruption before guest write", async () => {
  const cache = await mkdtemp(join(tmpdir(), "envd-artifact-test-"));
  const content = Buffer.from("fixture binary"), sha256 = createHash("sha256").update(content).digest("hex");
  const artifact = { url: "https://storage.googleapis.com/e2b-artifact-binaries/envd/v0.10.2/envd", sha256 };
  const calls: unknown[] = [];
  const vm = { sync: async (...args: unknown[]) => { calls.push(args); }, run: async () => ({ exitCode: 0 }) };
  try {
    await writeFile(join(cache, sha256), content);
    const path = await stageEnvdArtifact(vm as any, "/root/.arker/e2b-envd", artifact, cache);
    assert.deepEqual(calls, [[path, content]]);
    assert.ok(path.includes("/.candidate-") && !path.endsWith(sha256));
    const second = await stageEnvdArtifact(vm as any, "/root/.arker/e2b-envd", artifact, cache);
    assert.notEqual(second, path);
    await writeFile(join(cache, sha256), "corrupt");
    await assert.rejects(stageEnvdArtifact(vm as any, "/root/.arker/e2b-envd", artifact, cache), /checksum/);
    assert.equal(calls.length, 2);
  } finally { await rm(cache, { recursive: true }); }
});

test("oversized sparse cache is rejected by bounded descriptor reads before guest writes", async () => {
  const { open } = await import("node:fs/promises");
  const cache = await mkdtemp(join(tmpdir(), "envd-cache-limit-"));
  const sha256 = "a".repeat(64);
  const file = await open(join(cache, sha256), "w");
  await file.truncate(128 * 1024 * 1024 + 1); await file.close();
  try {
    await assert.rejects(stageEnvdArtifact({ sync: () => assert.fail("must not write") } as any,
      "/root/.arker/e2b-envd", { sha256, url: "https://storage.googleapis.com/e2b-artifact-binaries/envd/v0.10.2/envd" }, cache), /size limit/);
  } finally { await rm(cache, { recursive: true }); }
});

for (const failure of ["sync", "chmod-exit", "chmod-error", "cleanup-error"] as const) {
  test(`staging cleans only its candidate after ${failure} and preserves the original error`, async () => {
    const cache = await mkdtemp(join(tmpdir(), "envd-artifact-failure-"));
    const content = Buffer.from("fixture binary"), sha256 = createHash("sha256").update(content).digest("hex");
    const artifact = { url: "https://storage.googleapis.com/e2b-artifact-binaries/envd/v0.10.2/envd", sha256 };
    const installed = `/root/.arker/e2b-envd/bin/${sha256}`;
    const unrelated = "/root/.arker/e2b-envd/bin/.candidate-unrelated";
    const guestFiles = new Set([installed, unrelated]);
    const originalError = new Error("staging failed");
    let candidate = "";
    const cleanup: string[] = [];
    const vm = {
      sync: async (path: string) => {
        candidate = path;
        guestFiles.add(path);
        if (failure === "sync") throw originalError;
      },
      run: async (command: string) => {
        if (command === `rm -f -- ${candidate}`) {
          cleanup.push(command);
          if (failure === "cleanup-error") throw new Error("guest unavailable during cleanup");
          guestFiles.delete(candidate);
          return { exitCode: 0 };
        }
        assert.equal(command, `chmod 0700 -- ${candidate}`);
        if (failure === "chmod-exit") return { exitCode: 1 };
        throw originalError;
      },
    };
    try {
      await writeFile(join(cache, sha256), content);
      await assert.rejects(stageEnvdArtifact(vm as any, "/root/.arker/e2b-envd", artifact, cache),
        error => failure === "chmod-exit" ? /permissions/.test(String(error)) : error === originalError);
      assert.deepEqual(cleanup, [`rm -f -- ${candidate}`]);
      assert.equal(guestFiles.has(candidate), failure === "cleanup-error");
      assert.ok(guestFiles.has(installed) && guestFiles.has(unrelated));
    } finally { await rm(cache, { recursive: true }); }
  });
}
