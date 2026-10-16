import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { SandboxInstance } from "../src/blaxel.js";

if (!process.env.BL_API_KEY || !process.env.BL_WORKSPACE) {
  throw new Error("BL_API_KEY and BL_WORKSPACE are required");
}

const directory = await mkdtemp(join(tmpdir(), "arker-blaxel-live-"));
const routerConfigPath = join(directory, "computesdk_router.json");
await writeFile(routerConfigPath, JSON.stringify({
  version: 1,
  existing: { strategy: "priority", order: ["blaxel"] },
  create: { strategy: "priority", order: ["blaxel"] },
  cache: { path: join(directory, "routes.json"), maxEntries: 20, maxBytes: 16_384 },
}));

const name = `arker-blaxel-${crypto.randomUUID().slice(0, 12)}`;
const sandbox = await SandboxInstance.createIfNotExists(
  { name, image: "blaxel/base-image:latest", memory: 4096, region: "us-pdx-1", ports: [{ target: 8080, protocol: "HTTP" }], labels: { suite: "arker-shim" } },
  { timeout: 50, routerConfigPath },
);
try {
  assert.equal(sandbox.provider, "blaxel");
  assert.equal(sandbox.metadata.name, name);

  const result = await sandbox.process.exec({
    command: "pwd && printf '%s' \"$ARKER_BLAXEL_TEST\"",
    workingDir: "/blaxel",
    env: { ARKER_BLAXEL_TEST: "native-ok" },
    waitForCompletion: true,
  } as any);
  assert.equal(result.exitCode, 0);
  assert.match(result.stdout, /\/blaxel/);
  assert.match(result.stdout, /native-ok/);

  await sandbox.process.exec({ command: "python3 -m http.server 8080 --bind 0.0.0.0", waitForCompletion: false } as any);
  const inboundDeadline = Date.now() + 120_000;
  let inboundReached = false;
  while (Date.now() < inboundDeadline) {
    try {
      const response = await sandbox.fetch(8080, "/", { signal: AbortSignal.timeout(10_000) });
      if (response.ok) {
        inboundReached = true;
        break;
      }
    } catch {
      // The proxy can become ready shortly after the guest listener.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  assert.equal(inboundReached, true, "Blaxel native fetch did not reach the background daemon");

  await sandbox.fs.mkdir("/blaxel/arker-shim");
  await sandbox.fs.write("/blaxel/arker-shim/probe.txt", "persistent");
  assert.equal(await sandbox.fs.read("/blaxel/arker-shim/probe.txt"), "persistent");
  const listed = await sandbox.fs.ls("/blaxel/arker-shim");
  assert.ok(listed.files.some((file: any) => file.name === "probe.txt"));
  const found = await sandbox.fs.find("/blaxel/arker-shim", { patterns: ["*.txt"], type: "file" });
  assert.ok(found.matches?.some((file: any) => file.path.endsWith("probe.txt")));

  const reconnected = await SandboxInstance.get(name, { routerConfigPath });
  assert.equal(await reconnected.fs.read("/blaxel/arker-shim/probe.txt"), "persistent");
  assert.equal(typeof reconnected.fetch, "function", "native extended methods must remain available");

  const arkerKey = process.env.ARKER_API_KEY;
  delete process.env.ARKER_API_KEY;
  try {
    const nativeOnlyDefault = await SandboxInstance.get(name);
    assert.equal(nativeOnlyDefault.provider, "blaxel");
    assert.equal(await nativeOnlyDefault.fs.read("/blaxel/arker-shim/probe.txt"), "persistent");
  } finally {
    if (arkerKey) process.env.ARKER_API_KEY = arkerKey;
  }
} finally {
  await sandbox.delete();
}

await assert.rejects(
  SandboxInstance.create(
    { name: `invalid-${crypto.randomUUID().slice(0, 12)}`, region: "us-pdx-1" },
    { apiKey: "bl_invalid_key", workspace: process.env.BL_WORKSPACE, routerConfigPath },
  ),
  /auth|unauthorized|forbidden|api key|rejected|not found/i,
  "invalid Blaxel credentials must fail instead of being routed elsewhere",
);

console.log("PASS Blaxel native drop-in surface");
