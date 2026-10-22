import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import { Arker } from "../dist/index.js";

if (!process.env.ARKER_API_KEY) throw new Error("ARKER_API_KEY is required");
const client = new Arker();
const vm = await client.fork({ source_vm_name: "ubuntu" });
try {
  const path = "/tmp/secret-sync-fixture";
  const bytes = randomBytes(64);
  await vm.sync(path, "ordinary");
  await vm.sync(path, bytes, { isSecret: true });
  assert.deepEqual(Buffer.from(await vm.sync(path)), bytes);
  const mode = await vm.run(`stat -c %a ${path}`);
  assert.equal(mode.type, "completed");
  if (mode.type !== "completed") throw new Error("mode check did not complete");
  assert.equal(mode.exitCode, 0);
  assert.equal(mode.stdout.trim(), "600");
  const cli = spawnSync(process.execPath, [
    "../cli/dist/cli.js", "sync", vm.id, path, "-", "--secret", "--json",
  ], { input: bytes, encoding: "utf8", timeout: 60_000, env: process.env });
  assert.equal(cli.status, 0, "secret-aware CLI upload failed");
  assert.ok(!cli.stdout.includes(bytes.toString("base64")));
  assert.deepEqual(Buffer.from(await vm.sync(path)), bytes);
  assert.equal(JSON.parse(cli.stdout).bytes, bytes.length);
  console.log("PASS owned live SDK and CLI secret uploads preserve bytes and mode0600");
} finally {
  await vm.delete();
}
