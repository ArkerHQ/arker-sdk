// Live tests for tags, listing, names, filesystem snapshots, network
// allowlists and termination in the Modal compatibility layer. Run with
// ARKER_API_KEY set.
import assert from "node:assert/strict";

import { Arker } from "../src/index.js";
import { InvalidError, ModalClient, NotFoundError, type Sandbox } from "../src/modal.js";
import { reachable } from "./helpers/reachable.js";
import { section } from "./helpers/sections.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP modal lifecycle live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const run = crypto.randomUUID().slice(0, 8);
const modal = new ModalClient();
const app = await modal.apps.fromName("arker-e2e");

async function ids(sandboxes: AsyncIterable<Sandbox>): Promise<string[]> {
  const found: string[] = [];
  for await (const sandbox of sandboxes) found.push(sandbox.sandboxId);
  return found;
}

async function output(sandbox: Sandbox, command: string[]): Promise<string> {
  return (await sandbox.exec(command)).stdout.readText();
}

const sb = await modal.sandboxes.create(app, undefined, {
  name: `modal-e2e-${run}`,
  tags: { run },
  env: { SANDBOX_VAR: "kept" },
  workdir: "/tmp",
  timeoutMs: 900_000,
});
try {
  await section("tags, list and names", async () => {
    assert.deepEqual(await ids(modal.sandboxes.list({ tags: { run } })), [sb.sandboxId]);
    await sb.setTags({ run, stage: "two" });
    assert.deepEqual(await (await modal.sandboxes.fromId(sb.sandboxId)).getTags(), { run, stage: "two" });
    assert.deepEqual(await ids(modal.sandboxes.list({ tags: { run, stage: "two" } })), [sb.sandboxId]);
    assert.deepEqual(await ids(modal.sandboxes.list({ tags: { run, stage: "three" } })), []);
    const named = await modal.sandboxes.fromName("any-app", `modal-e2e-${run}`);
    assert.equal(named.sandboxId, sb.sandboxId);
    assert.equal(await output(named, ["pwd"]), "/tmp\n");
    await assert.rejects(modal.sandboxes.fromName("any-app", `no-such-sandbox-${run}`), NotFoundError);
  });

  await section("a filesystem snapshot is an image", async () => {
    const file = await sb.open("/tmp/state.txt", "w");
    await file.write("before");
    await file.close();
    const image = await sb.snapshotFilesystem({ ttlMs: 3_600_000 });
    try {
      const rules = (await new Arker().getVm(image.imageId!)).policies?.rules as Record<string, { after_created?: number }>;
      assert.equal(rules["compat-timeout"]?.after_created, 3600);
      assert.ok(!(await ids(modal.sandboxes.list({ tags: { run } }))).includes(image.imageId!));
      assert.equal(await image.build(app), image);
      await assert.rejects(modal.images.fromId(sb.sandboxId), NotFoundError);
      const restored = await modal.sandboxes.create(app, await modal.images.fromId(image.imageId!), { timeoutMs: 300_000 });
      try {
        assert.equal(await output(restored, ["cat", "/tmp/state.txt"]), "before");
        assert.equal(await output(restored, ["sh", "-c", "echo $SANDBOX_VAR"]), "kept\n");
      } finally {
        await restored.terminate();
      }
    } finally {
      await (await new Arker().getVm(image.imageId!)).delete();
    }
  });
} finally {
  await sb.terminate();
}

await section("outbound allowlists", async () => {
  const sandbox = await modal.sandboxes.create(app, undefined, {
    outboundCidrAllowlist: ["1.1.1.1/32"],
    outboundDomainAllowlist: ["example.com"],
    timeoutMs: 300_000,
  });
  try {
    const check = (cmd: string) => output(sandbox, ["bash", "-c", cmd]);
    assert.deepEqual(await reachable(check, "https://1.1.1.1", "https://example.com", "https://github.com"), {
      "https://1.1.1.1": true,
      "https://example.com": true,
      "https://github.com": false,
    });
  } finally {
    await sandbox.terminate();
  }
});

await section("inbound cidr allowlist", async () => {
  const sandbox = await modal.sandboxes.create(app, undefined, {
    encryptedPorts: [8000],
    inboundCidrAllowlist: ["192.0.2.0/24"],
    timeoutMs: 300_000,
  });
  try {
    await sandbox.exec(["python3", "-m", "http.server", "8000"], { workdir: "/tmp" });
    assert.equal(await output(sandbox, ["bash", "-c", "sleep 2; curl -s -o /dev/null -w '%{http_code}' localhost:8000"]), "200");
    const url = (await (await modal.sandboxes.fromId(sandbox.sandboxId)).tunnels())[8000]!.url;
    const answer = await fetch(url, { signal: AbortSignal.timeout(10_000) }).then(
      (response) => response.status,
      () => "refused",
    );
    assert.notEqual(answer, 200);
  } finally {
    await sandbox.terminate();
  }
});

await section("ready, detach and terminate", async () => {
  const finished = await modal.sandboxes.create(app, undefined, { command: ["true"], timeoutMs: 300_000 });
  await finished.waitUntilReady();
  await finished.wait();
  finished.detach();
  assert.equal(await finished.terminate({ wait: true }), 0);
  const running = await modal.sandboxes.create(app, undefined, { timeoutMs: 300_000 });
  assert.equal(await running.terminate({ wait: true }), 137);
  assert.equal(await running.poll(), 137);
  await assert.rejects(running.waitUntilReady(0), InvalidError);
});

console.log("PASS modal lifecycle live");
