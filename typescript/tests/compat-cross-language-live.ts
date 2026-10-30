import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { SandboxInstance as Native } from "@blaxel/core";
import { SandboxInstance as Shim } from "../src/blaxel.js";

const pythonProgram = String.raw`
import json,sys
from blaxel.core.sandbox import SyncSandboxInstance, ProcessRequestWithLog
phase,name=sys.argv[1:]
if phase == "create":
    sandbox=SyncSandboxInstance.create_if_not_exists({"name":name})
    try:
        result=sandbox.process.exec(ProcessRequestWithLog(
            command="cat; printf python-error >&2", name="python-started", stdin=True, wait_for_completion=False))
        print(json.dumps({"pid":result.pid,"status":result.status,"id":getattr(sandbox,"id",name)}))
    except BaseException:
        sandbox.delete()
        raise
else:
    sandbox=SyncSandboxInstance.get(name)
    if phase == "finish":
        sandbox.process.write_stdin("typescript-started", "from Python λ\n")
        sandbox.process.close_stdin("typescript-started")
        sandbox.process.wait("typescript-started",max_wait=10000,interval=100)
    result=sandbox.process.get("python-started" if phase == "verify" else "typescript-started")
    print(json.dumps({"stdout":result.stdout,"stderr":result.stderr,"status":result.status}))
`;

async function python(phase: string, name: string, shim: boolean): Promise<any> {
  const source = shim ? pythonProgram.replace(
    "from blaxel.core.sandbox import SyncSandboxInstance, ProcessRequestWithLog",
    "from arker.blaxel import SyncSandboxInstance\nfrom blaxel.core.sandbox import ProcessRequestWithLog",
  ) : pythonProgram;
  return new Promise((resolve, reject) => {
    const child = spawn("uv", ["run", "python", "-c", source, phase, name], {
      cwd: fileURLToPath(new URL("../../python", import.meta.url)),
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) { reject(new Error(`Python ${phase} failed: ${stderr}`)); return; }
      try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); }
    });
  });
}

if (!process.env.ARKER_API_KEY || !process.env.BL_API_KEY) throw new Error("Live credentials required");
const directory = await mkdtemp(join(tmpdir(), "arker-cross-language-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
try {
  for (const [provider, constructor, shim] of [["blaxel", Native, false], ["blaxel", Shim, true], ["arker", Shim, true]] as const) {
    const config = join(directory, "router.json");
    await writeFile(config, JSON.stringify({
      version: 1,
      existing: { strategy: "priority", order: [provider] },
      create: { strategy: "priority", order: [provider] },
      cache: { path: join(directory, "routes.json"), maxEntries: 100, maxBytes: 65536 },
    }));
    process.env.COMPUTESDK_ROUTER_CONFIG = config;
    const name = `arker-cross-${crypto.randomUUID().slice(0, 12)}`;
    const created = await python("create", name, shim);
    try {
      if (shim) {
        const cache = JSON.parse(await readFile(join(directory, "routes.json"), "utf8"));
        assert.equal(cache.entries[created.id].provider, provider, "Python creation must persist provider ownership");
      }
      const sandbox = await constructor.get(created.id);
      const existing = await sandbox.process.get("python-started");
      assert.equal(existing.status, "running");
      assert.equal(existing.pid, created.pid);
      await sandbox.process.writeStdin("python-started", "from TypeScript λ\n");
      await sandbox.process.closeStdin("python-started");
      const first = await sandbox.process.wait("python-started", { maxWait: 10_000, interval: 100 });
      assert.equal(first.stdout, "from TypeScript λ\n");
      assert.equal(first.stderr, "python-error");
      assert.equal((await python("verify", name, shim)).stdout, first.stdout);
      await sandbox.process.exec({ command: "cat; printf typescript-error >&2", name: "typescript-started", stdin: true, waitForCompletion: false });
      const second = await python("finish", name, shim);
      assert.equal(second.stdout, "from Python λ\n");
      const observed = await sandbox.process.get("typescript-started");
      assert.equal(observed.stdout, second.stdout);
      assert.equal(observed.stderr, "typescript-error");
      assert.equal(observed.status, "completed");
      console.log(`PASS cross-language process control: ${shim ? "shim" : "native SDK"} on ${provider}`);
    } finally {
      await constructor.delete(name);
    }
  }
} finally {
  if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
  else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
  await rm(directory, { recursive: true, force: true });
}
