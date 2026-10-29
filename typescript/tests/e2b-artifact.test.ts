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

for (const failure of ["sync", "chmod-exit", "chmod-error", "cleanup-error", "cleanup-exit"] as const) {
  test(`staging cleans only its candidate after ${failure} and preserves the original error`, async () => {
    const cache = await mkdtemp(join(tmpdir(), "envd-artifact-failure-"));
    const content = Buffer.from("fixture binary"), sha256 = createHash("sha256").update(content).digest("hex");
    const artifact = { url: "https://storage.googleapis.com/e2b-artifact-binaries/envd/v0.10.2/envd", sha256 };
    const installed = `/root/.arker/e2b-envd/bin/${sha256}`;
    const unrelated = "/root/.arker/e2b-envd/bin/.candidate-unrelated";
    const guestFiles = new Set([installed, unrelated]);
    const originalError = new Error("staging failed");
    const cleanupError = new Error("guest unavailable during cleanup");
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
          if (failure === "cleanup-error") throw cleanupError;
          if (failure === "cleanup-exit") return { exitCode: 1 };
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
        error => failure.startsWith("cleanup-")
          ? error instanceof AggregateError && error.errors[0] === originalError &&
            (failure === "cleanup-error" ? error.errors[1] === cleanupError : /cleanup failed/.test(String(error.errors[1])))
          : failure === "chmod-exit" ? /permissions/.test(String(error)) : error === originalError);
      assert.deepEqual(cleanup, [`rm -f -- ${candidate}`]);
      assert.equal(guestFiles.has(candidate), failure.startsWith("cleanup-"));
      assert.ok(guestFiles.has(installed) && guestFiles.has(unrelated));
    } finally { await rm(cache, { recursive: true }); }
  });
}

for (const target of [
  "https://release-assets.githubusercontent.com/github-production-release-asset/123/binary?signature=public",
  "https://example.com/binary",
  "http://release-assets.githubusercontent.com/binary",
  "https://release-assets.githubusercontent.com.evil.example/binary",
  "https://user:password@release-assets.githubusercontent.com/binary",
  "https://release-assets.githubusercontent.com:444/binary",
  "missing", "loop", "corrupt",
]) {
  test(`cold pinned release download confines redirects: ${target}`, async () => {
    const { stagePinnedArtifact } = await import("../src/compat/e2b-artifact.js");
    const cache = await mkdtemp(join(tmpdir(), "artifact-redirect-"));
    const content = Buffer.from("public fixture artifact");
    const artifact = { url: "https://github.com/blaxel-ai/sandbox/releases/download/v0.2.74/sandbox-api-linux-amd64",
      sha256: createHash("sha256").update(content).digest("hex") };
    const original = globalThis.fetch;
    const requests: string[] = []; const writes: unknown[] = [];
    globalThis.fetch = (async (input: string | URL | Request, options?: RequestInit) => {
      requests.push(String(input));
      assert.equal(options?.redirect, "manual");
      assert.equal(options?.credentials, "omit");
      const headers: string[] = [];
      new Headers(options?.headers).forEach((_value, name) => headers.push(name));
      assert.deepEqual(headers, []);
      if (requests.length === 1 || target === "loop") return new Response(null, {
        status: 302, headers: target === "missing" ? {} : { location: target === "loop" || target === "corrupt"
          ? "https://release-assets.githubusercontent.com/binary" : target },
      });
      return new Response(target === "corrupt" ? "wrong bytes" : content);
    }) as typeof fetch;
    try {
      const promise = stagePinnedArtifact({ sync: async (...args: unknown[]) => { writes.push(args); },
        run: async () => ({ exitCode: 0 }) } as any, "/root/.arker/blaxel", artifact, cache);
      if (target.includes("github-production-release-asset")) {
        await promise; assert.equal(requests.length, 2); assert.equal(writes.length, 1);
      } else {
        await assert.rejects(promise, target === "corrupt" ? /checksum/ : /redirect rejected/); assert.equal(writes.length, 0);
        assert.equal(requests.length, target === "loop" ? 4 : target === "corrupt" ? 2 : 1);
      }
    } finally { globalThis.fetch = original; await rm(cache, { recursive: true }); }
  });
}
