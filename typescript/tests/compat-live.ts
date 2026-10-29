import assert from "node:assert/strict";

import { Arker } from "../src/index.js";
import { SandboxInstance as BlaxelSandbox } from "../src/blaxel.js";
import { Daytona } from "../src/daytona.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { ModalClient } from "../src/modal.js";

const apiKey = process.env.ARKER_API_KEY;
if (!apiKey) {
  console.log("SKIP compat live: set ARKER_API_KEY to run");
  process.exit(0);
}

const provider = process.env.ARKER_PROVIDER ?? "aws";
const region = process.env.ARKER_REGION ?? "us-east-1";
const arker = new Arker({ apiKey, provider, region });

async function expectArkerVm(id: string): Promise<void> {
  const vm = await arker.getVm(id);
  assert.equal(vm.id, id);
}

async function testDaytonaLive(): Promise<void> {
  const daytona = new Daytona({ arker: { apiKey } });
  const sandbox = await daytona.create();
  try {
    await expectArkerVm(sandbox.id);
    const result = await sandbox.process.exec("printf daytona-live");
    assert.equal(result.result, "daytona-live");
  } finally {
    await sandbox.delete();
  }
}

async function testE2BLive(): Promise<void> {
  const sandbox = await E2BSandbox.create();
  try {
    await expectArkerVm(sandbox.sandboxId);
    const result = await sandbox.commands.run("printf e2b-live");
    assert.equal(result.stdout, "e2b-live");
    await sandbox.files.write("/tmp/arker-e2b-live.txt", "ok");
    assert.equal(await sandbox.files.read("/tmp/arker-e2b-live.txt"), "ok");
  } finally {
    await sandbox.kill();
  }
}

async function testModalLive(): Promise<void> {
  const client = new ModalClient({ arker: { apiKey } });
  const sandbox = await client.sandboxes.create();
  try {
    await expectArkerVm(sandbox.sandboxId);
    const proc = await sandbox.exec(["sh", "-c", "printf modal-live"]);
    assert.equal(await proc.stdout.readText(), "modal-live");
    assert.equal(await proc.wait(), 0);
  } finally {
    await sandbox.terminate();
  }
}

async function testBlaxelLive(): Promise<void> {
  const sandbox = await BlaxelSandbox.create({}, { arker: { apiKey } });
  try {
    await expectArkerVm(sandbox.id);
    const result = await sandbox.process.exec({ command: "printf blaxel-live", waitForCompletion: true });
    assert.equal(result.stdout, "blaxel-live");
    await sandbox.fs.write("/tmp/arker-blaxel-live.txt", "ok");
    assert.equal(await sandbox.fs.read("/tmp/arker-blaxel-live.txt"), "ok");
  } finally {
    await sandbox.delete();
  }
}

await testDaytonaLive();
await testE2BLive();
await testModalLive();
await testBlaxelLive();

console.log("PASS compat live");
