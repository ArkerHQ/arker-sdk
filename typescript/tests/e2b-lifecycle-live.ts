// Live tests for listing, snapshots, forks, pausing and network updates
// in the E2B compatibility layer. Run with ARKER_API_KEY set.
import assert from "node:assert/strict";

import { Arker } from "../src/index.js";
import { Daytona } from "../src/daytona.js";
import { ALL_TRAFFIC, NotSupportedError, Sandbox, type SandboxPaginator, type SandboxInfo } from "../src/e2b.js";
import { reachable } from "./helpers/reachable.js";
import { section } from "./helpers/sections.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP e2b lifecycle live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const run = crypto.randomUUID().slice(0, 8);

async function all(paginator: SandboxPaginator): Promise<SandboxInfo[][]> {
  const pages: SandboxInfo[][] = [];
  while (paginator.hasNext) pages.push(await paginator.nextItems());
  return pages;
}

const sbx = await Sandbox.create({ envs: { SANDBOX_VAR: "kept" }, metadata: { run, role: "first" }, timeoutMs: 900_000 });
try {
  await section("list pages through this layer's sandboxes", async () => {
    const second = await Sandbox.create({ metadata: { run, role: "second" }, timeoutMs: 300_000 });
    try {
      const pages = await all(Sandbox.list({ query: { metadata: { run }, state: ["running"] }, limit: 1 }));
      assert.deepEqual(pages.map((page) => page.map((info) => info.sandboxId)), [[sbx.sandboxId], [second.sandboxId]]);
      assert.deepEqual(pages[0]![0]!.metadata, { run, role: "first" });
      const onlySecond = await Sandbox.list({ query: { metadata: { run, role: "second" } } }).nextItems();
      assert.deepEqual(onlySecond.map((info) => info.sandboxId), [second.sandboxId]);
      assert.deepEqual(await Sandbox.list({ query: { metadata: { run }, state: ["paused"] } }).nextItems(), []);
      // A nextToken resumes a new listing where the last page ended, in either order.
      const firstPage = Sandbox.list({ query: { metadata: { run } }, limit: 1 });
      assert.deepEqual((await firstPage.nextItems()).map((info) => info.sandboxId), [sbx.sandboxId]);
      const resumed = Sandbox.list({ query: { metadata: { run } }, limit: 1, nextToken: firstPage.nextToken });
      assert.deepEqual((await resumed.nextItems()).map((info) => info.sandboxId), [second.sandboxId]);
      assert.deepEqual([resumed.hasNext, resumed.nextToken], [false, undefined]);
      const newest = Sandbox.list({ query: { metadata: { run } }, limit: 1, order: "desc" });
      assert.deepEqual((await newest.nextItems()).map((info) => info.sandboxId), [second.sandboxId]);
      const older = Sandbox.list({ query: { metadata: { run } }, order: "desc", nextToken: newest.nextToken });
      assert.deepEqual((await older.nextItems()).map((info) => info.sandboxId), [sbx.sandboxId]);
    } finally {
      await second.kill();
    }
  });

  await section("list leaves out other layers' sandboxes", async () => {
    const other = await new Daytona().create();
    try {
      const listed = (await all(Sandbox.list())).flat().map((info) => info.sandboxId);
      assert.ok(listed.includes(sbx.sandboxId));
      assert.ok(!listed.includes(other.id));
    } finally {
      await other.delete();
    }
  });

  await section("snapshot, fork and create from a snapshot", async () => {
    await sbx.files.write("state.txt", "before");
    const snapshot = await sbx.createSnapshot();
    try {
      const snapshots = await sbx.listSnapshots().nextItems();
      assert.ok(snapshots.some((info) => info.snapshotId === snapshot.snapshotId));
      const listed = await Sandbox.list({ query: { metadata: { run, role: "first" } } }).nextItems();
      assert.deepEqual(listed.map((info) => info.sandboxId), [sbx.sandboxId], "a snapshot is not listed as a sandbox");
      assert.ok(!("compat-timeout" in ((await new Arker().getVm(snapshot.snapshotId)).policies?.rules ?? {})));
      await sbx.files.write("state.txt", "after");
      const restored = await Sandbox.create(snapshot.snapshotId, { timeoutMs: 300_000 });
      try {
        assert.equal(await restored.files.read("state.txt"), "before");
        assert.equal((await restored.commands.run("echo $SANDBOX_VAR $E2B_SANDBOX_ID")).stdout, `kept ${restored.sandboxId}\n`);
      } finally {
        await restored.kill();
      }
      const [fork] = await sbx.fork({ timeoutMs: 300_000 });
      assert.ok(fork instanceof Sandbox);
      try {
        assert.equal(await fork.files.read("state.txt"), "after");
        assert.equal((await fork.commands.run("echo $SANDBOX_VAR $E2B_SANDBOX_ID")).stdout, `kept ${fork.sandboxId}\n`);
        const rules = (await new Arker().getVm(fork.sandboxId)).policies?.rules as Record<string, { after_created?: number }>;
        assert.equal(rules["compat-timeout"]?.after_created, 300);
      } finally {
        await fork.kill();
      }
    } finally {
      assert.ok(await Sandbox.deleteSnapshot(snapshot.snapshotId));
    }
    assert.equal(await Sandbox.deleteSnapshot(snapshot.snapshotId), false);
    assert.equal(await Sandbox.deleteSnapshot(sbx.sandboxId), false, "a sandbox is not a snapshot");
    assert.ok(await sbx.isRunning());
  });

  await section("pause suspends and connect resumes", async () => {
    await sbx.commands.run("sleep 120", { background: true, timeoutMs: 0 });
    assert.equal((await sbx.arker.refresh()).state, "running");
    assert.ok(await Sandbox.pause(sbx.sandboxId, { keepMemory: true }));
    const deadline = Date.now() + 30_000;
    while ((await sbx.arker.refresh()).state !== "idle") {
      assert.ok(Date.now() < deadline, "pause did not suspend the sandbox");
      await sleep(1_000);
    }
    assert.ok(!("compat-timeout" in ((await sbx.arker.refresh()).policies?.rules ?? {})));
    const resumed = await Sandbox.connect(sbx.sandboxId, { timeoutMs: 900_000 });
    assert.equal((await resumed.commands.run("cat state.txt")).stdout, "after");
    await assert.rejects(sbx.connect({ onResume: "reboot" }), NotSupportedError);
  });

  await section("update network replaces the rules", async () => {
    const check = async () =>
      reachable(async (cmd) => (await sbx.commands.run(cmd)).stdout, "https://1.1.1.1", "https://example.com");
    await sbx.updateNetwork({ allowOut: ["1.1.1.1"], denyOut: [ALL_TRAFFIC] });
    assert.deepEqual(await check(), { "https://1.1.1.1": true, "https://example.com": false });
    await Sandbox.updateNetwork(sbx.sandboxId, { denyOut: ["1.1.1.1"] });
    assert.deepEqual(await check(), { "https://1.1.1.1": false, "https://example.com": true });
    await sbx.updateNetwork({});
    assert.deepEqual(await check(), { "https://1.1.1.1": true, "https://example.com": true });
  });
} finally {
  await sbx.kill();
}

console.log("PASS e2b lifecycle live");
