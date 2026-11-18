// Live tests for listing, names, start and stop, labels, network updates,
// resizing, environment, snapshots and forks in the Daytona
// compatibility layer. Run with ARKER_API_KEY set.
import assert from "node:assert/strict";

import { Arker } from "../src/index.js";
import { Daytona, DaytonaNotFoundError, DaytonaNotSupportedError, DaytonaValidationError, type Sandbox } from "../src/daytona.js";
import { reachable } from "./helpers/reachable.js";
import { section } from "./helpers/sections.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP daytona lifecycle live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const run = crypto.randomUUID().slice(0, 8);
const daytona = new Daytona();

async function ids(sandboxes: AsyncIterable<Sandbox>): Promise<string[]> {
  const found: string[] = [];
  for await (const sandbox of sandboxes) found.push(sandbox.id);
  return found;
}

async function status(url: string): Promise<number | undefined> {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(10_000) })).status;
  } catch {
    return undefined;
  }
}

async function assertSuspends(sandbox: Sandbox, action: () => Promise<void>, name: string): Promise<void> {
  const session = await sandbox.arker.createSession();
  await sandbox.arker.run("sleep 120", { session_id: session.session_id, time_to_background: 0 });
  assert.equal((await sandbox.arker.refresh()).state, "running");
  await action();
  const deadline = Date.now() + 30_000;
  while ((await sandbox.arker.refresh()).state !== "idle") {
    assert.ok(Date.now() < deadline, `${name} did not suspend the sandbox`);
    await sleep(1_000);
  }
}

const sandbox = await daytona.create({ name: `daytona-e2e-${run}`, labels: { run }, envVars: { SANDBOX_VAR: "kept" } });
try {
  await section("list, labels and names", async () => {
    assert.deepEqual(await ids(daytona.list({ labels: { run } })), [sandbox.id]);
    assert.deepEqual(await sandbox.setLabels({ run, stage: "two" }), { run, stage: "two" });
    assert.deepEqual(await ids(daytona.list({ labels: { stage: "two", run } })), [sandbox.id]);
    assert.deepEqual(await ids(daytona.list({ labels: { run, stage: "three" } })), []);
    assert.deepEqual(await ids(daytona.list({ name: `DAYTONA-E2E-${run}` })), [sandbox.id]);
    assert.deepEqual(await ids(daytona.list({ name: `no-such-${run}` })), []);
    const named = await daytona.get(`daytona-e2e-${run}`);
    assert.equal(named.id, sandbox.id);
    assert.deepEqual(named.labels, { run, stage: "two" });
    assert.deepEqual(named.env, { SANDBOX_VAR: "kept" });
    const mine = { labels: { run } };
    assert.deepEqual(await ids(daytona.list({ ...mine, states: ["started"] })), [sandbox.id]);
    assert.deepEqual(await ids(daytona.list({ ...mine, states: ["archived"] })), []);
    assert.deepEqual(await ids(daytona.list({ ...mine, isPublic: false })), [sandbox.id]);
    assert.deepEqual(await ids(daytona.list({ ...mine, isPublic: true })), []);
    const created = new Date((await new Arker().getVm(sandbox.id)).created_at!);
    const minuteBefore = new Date(created.getTime() - 60_000);
    assert.deepEqual(await ids(daytona.list({ ...mine, createdAtAfter: minuteBefore })), [sandbox.id]);
    assert.deepEqual(await ids(daytona.list({ ...mine, createdAtBefore: minuteBefore })), []);
    assert.deepEqual(await ids(daytona.list({ ...mine, minCpu: 1, maxCpu: 64 })), [sandbox.id]);
    assert.deepEqual(await ids(daytona.list({ ...mine, minMemoryGib: 4096 })), []);
    await assert.rejects(ids(daytona.list({ autoDestroyAtAfter: created })), DaytonaNotSupportedError);
  });

  await section("stop, pause and archive suspend", async () => {
    await assertSuspends(sandbox, () => daytona.stop(sandbox), "stop");
    assert.equal(sandbox.state, "stopped");
    await sandbox.waitUntilStopped();
    await daytona.start(sandbox);
    await sandbox.waitUntilStarted();
    assert.equal(sandbox.state, "started");
    assert.equal((await sandbox.process.executeCommand("echo $SANDBOX_VAR")).result, "kept\n");
    await assertSuspends(sandbox, () => sandbox.pause(), "pause");
    assert.equal(sandbox.state, "paused");
    await assertSuspends(sandbox, () => sandbox.archive(), "archive");
    assert.equal(sandbox.state, "archived");
    await sandbox.recover();
    await sandbox.refreshActivity();
    assert.equal(sandbox.state, "started");
    assert.equal((await sandbox.process.executeCommand("echo $SANDBOX_VAR")).result, "kept\n");
  });

  await section("env and directories", async () => {
    await sandbox.updateEnv({ NEW_VAR: "it's new" }, { unset: ["SANDBOX_VAR"] });
    assert.equal((await sandbox.process.executeCommand('echo "$NEW_VAR|$SANDBOX_VAR"')).result, "it's new|\n");
    await sandbox.refreshData();
    assert.deepEqual(sandbox.env, { NEW_VAR: "it's new" });
    await sandbox.updateEnv({ SANDBOX_VAR: "kept" }, { unset: ["NEW_VAR"] });
    assert.equal(await sandbox.getUserHomeDir(), "/root");
    assert.equal(await sandbox.getUserRootDir(), "/root");
    assert.ok((await sandbox.getWorkDir()).startsWith("/"));
  });

  await section("set public opens existing ports", async () => {
    await sandbox.fs.uploadFile(Buffer.from("served"), "/tmp/www/index.html");
    await sandbox.process.executeCommand("cd /tmp/www && (nohup python3 -m http.server 3000 >/dev/null 2>&1 &)");
    const url = `${(await sandbox.getPreviewLink(3000)).url}/index.html`;
    assert.equal(await status(url), 401);
    await sandbox.setPublic(true);
    assert.ok(sandbox.public);
    assert.ok((await daytona.get(sandbox.id)).public);
    const deadline = Date.now() + 30_000;
    while ((await status(url)) !== 200) {
      assert.ok(Date.now() < deadline, `${url} never served without credentials`);
      await sleep(1_000);
    }
  });

  await section("update network settings", async () => {
    const check = () =>
      reachable(async (cmd) => (await sandbox.process.executeCommand(cmd)).result, "https://1.1.1.1", "https://example.com", "https://github.com");
    const other = await daytona.get(sandbox.id);
    await sandbox.updateNetworkSettings({ networkBlockAll: true });
    assert.equal(sandbox.networkBlockAll, true);
    assert.equal(other.networkBlockAll, false);
    await other.refreshData();
    assert.equal(other.networkBlockAll, true);
    assert.deepEqual(await check(), { "https://1.1.1.1": false, "https://example.com": false, "https://github.com": false });
    await sandbox.updateNetworkSettings({ networkAllowList: "1.1.1.1/32" });
    assert.equal(sandbox.networkBlockAll, false);
    assert.deepEqual(await check(), { "https://1.1.1.1": true, "https://example.com": false, "https://github.com": false });
    await sandbox.updateNetworkSettings({ domainAllowList: "example.com" });
    assert.deepEqual(await check(), { "https://1.1.1.1": false, "https://example.com": true, "https://github.com": false });
    await sandbox.updateNetworkSettings({ networkBlockAll: false });
    assert.deepEqual(await check(), { "https://1.1.1.1": true, "https://example.com": true, "https://github.com": true });
    await assert.rejects(sandbox.updateNetworkSettings({}), DaytonaValidationError);
  });

  await section("resize", async () => {
    await sandbox.resize({ cpu: 2, memory: 2 });
    const resources = (await new Arker().getVm(sandbox.id)).resources as { vcpu?: number; memory_mib?: number };
    assert.deepEqual([resources.vcpu, resources.memory_mib], [2, 2048]);
    await assert.rejects(sandbox.resize({ gpu: 1 }), DaytonaNotSupportedError);
  });

  await section("snapshot and fork", async () => {
    await sandbox.fs.uploadFile(Buffer.from("before"), "/tmp/state.txt");
    const name = `daytona-snapshot-${run}`;
    await sandbox.createSnapshot(name);
    try {
      const listed: (string | undefined)[] = [];
      for await (const each of daytona.list()) listed.push(each.name);
      assert.ok(!listed.includes(name));
      assert.ok(!("compat-timeout" in ((await new Arker().getVm(name)).policies?.rules ?? {})));
      await sandbox.fs.uploadFile(Buffer.from("after"), "/tmp/state.txt");
      const restored = await daytona.create({ snapshot: name });
      try {
        assert.equal((await restored.fs.downloadFile("/tmp/state.txt")).toString(), "before");
        assert.equal((await restored.process.executeCommand("echo $SANDBOX_VAR")).result, "kept\n");
      } finally {
        await restored.delete();
      }
      const fork = await daytona.fork(sandbox, { name: `daytona-fork-${run}` });
      try {
        assert.notEqual(fork.id, sandbox.id);
        assert.equal(fork.name, `daytona-fork-${run}`);
        assert.equal((await fork.fs.downloadFile("/tmp/state.txt")).toString(), "after");
        assert.deepEqual(fork.labels, sandbox.labels);
        assert.equal((await fork.process.executeCommand("echo $SANDBOX_VAR")).result, "kept\n");
      } finally {
        await fork.delete();
      }
    } finally {
      await (await new Arker().getVm(name)).delete();
    }
  });

  await section("list sorts and filters by snapshot", async () => {
    const name = `daytona-sorted-${run}`;
    await sandbox.createSnapshot(name);
    try {
      const second = await daytona.create({ snapshot: name, labels: { run } });
      try {
        assert.deepEqual(await ids(daytona.list({ labels: { run }, snapshots: [name] })), [second.id]);
        const ordered = [sandbox.id, second.id];
        assert.deepEqual(await ids(daytona.list({ labels: { run }, sort: "createdAt", order: "asc" })), ordered);
        assert.deepEqual(await ids(daytona.list({ labels: { run }, sort: "createdAt", order: "desc" })), ordered.reverse());
        await assert.rejects(ids(daytona.list({ sort: "colour" })), DaytonaValidationError);
      } finally {
        await second.delete();
      }
    } finally {
      await daytona.snapshot.delete(name);
    }
  });

  await section("ttl and idle intervals", async () => {
    const other = await daytona.create();
    try {
      await other.setTtl(30);
      const rule = (await new Arker().getVm(other.id)).policies?.rules?.["compat-timeout"] as { action: string; at: string };
      assert.equal(rule.action, "delete");
      const remaining = Date.parse(rule.at) - Date.now();
      assert.ok(remaining > 29 * 60_000 && remaining <= 30 * 60_000, `${remaining}`);
      await other.setTtl(0);
      assert.ok(!("compat-timeout" in ((await new Arker().getVm(other.id)).policies?.rules ?? {})));
      await assert.rejects(other.setTtl(-1), DaytonaValidationError);
      const warnings: string[] = [];
      const warn = console.warn;
      console.warn = (message: string) => void warnings.push(String(message));
      try {
        await other.setAutostopInterval(15);
        await other.setAutoPauseInterval(15);
        await other.setAutoArchiveInterval(15);
        await other.setAutoDeleteInterval(-1);
      } finally {
        console.warn = warn;
      }
      for (const method of ["setAutostopInterval", "setAutoPauseInterval", "setAutoArchiveInterval", "setAutoDeleteInterval"]) {
        assert.ok(warnings.some((warning) => warning.includes(`Sandbox.${method}`)), method);
      }
      await other.waitForResizeComplete();
    } finally {
      await other.delete();
    }
  });

  await section("snapshot service", async () => {
    const name = `daytona-image-${run}`;
    const logs: string[] = [];
    const snapshot = await daytona.snapshot.create({ name, image: "node:22-slim", resources: { cpu: 2 } }, { onLogs: (chunk) => void logs.push(chunk) });
    try {
      assert.deepEqual([snapshot.name, snapshot.imageName, snapshot.state, snapshot.cpu], [name, "node:22-slim", "active", 2]);
      assert.ok(logs.length > 0);
      assert.equal((await daytona.snapshot.get(name)).id, snapshot.id);
      assert.equal((await daytona.snapshot.activate(name)).name, name);
      assert.ok((await daytona.snapshot.list({ limit: 1000 })).items.some((listed) => listed.name === name));
      const ofSandbox = `daytona-of-sandbox-${run}`;
      await sandbox.createSnapshot(ofSandbox);
      try {
        const page = await daytona.snapshot.list({ sourceSandboxId: sandbox.id, limit: 1 });
        assert.deepEqual([page.items.map((listed) => listed.name), page.total, page.totalPages], [[ofSandbox], 1, 1]);
        assert.equal((await daytona.snapshot.get(ofSandbox)).sourceSandboxId, sandbox.id);
      } finally {
        await daytona.snapshot.delete(ofSandbox);
      }
      const started = await daytona.create({ snapshot: name });
      try {
        assert.ok((await started.process.executeCommand("node --version")).result.startsWith("v22."));
      } finally {
        await started.delete();
      }
    } finally {
      await daytona.snapshot.delete(snapshot);
    }
    await assert.rejects(daytona.snapshot.get(name), DaytonaNotFoundError);
    await assert.rejects(daytona.snapshot.get(sandbox.id), DaytonaNotFoundError);
    await assert.rejects(daytona.snapshot.list({ page: 0 }), DaytonaValidationError);
  });

  await section("a sandbox from a snapshot takes none of its ports or network rules", async () => {
    const parent = await daytona.create({ public: true, networkAllowList: "1.1.1.1/32" });
    const name = `daytona-ports-${run}`;
    try {
      await parent.fs.uploadFile(Buffer.from("parent's page"), "/tmp/www/index.html");
      await parent.process.executeCommand("cd /tmp/www && (nohup python3 -m http.server 3000 >/dev/null 2>&1 &)");
      const url = `${(await parent.getPreviewLink(3000)).url}/index.html`;
      const deadline = Date.now() + 30_000;
      while ((await status(url)) !== 200) {
        assert.ok(Date.now() < deadline, `${url} never served without credentials`);
        await sleep(1_000);
      }
      await parent.createSnapshot(name);
      const child = await daytona.create({ snapshot: name });
      try {
        // The child runs the parent's server, but is not public and opened no port.
        assert.equal((await child.process.executeCommand("curl -s localhost:3000/index.html")).result, "parent's page");
        const { hostname } = (await new Arker().getVm(child.id)) as unknown as { hostname: string };
        const dot = hostname.indexOf(".");
        assert.notEqual(await status(`https://${hostname.slice(0, dot)}-3000${hostname.slice(dot)}/index.html`), 200);
        const rules = Object.keys((await new Arker().getVm(child.id)).policies?.rules ?? {});
        assert.deepEqual(rules.filter((id) => id.startsWith("compat-")), []);
        assert.deepEqual(await reachable(async (cmd) => (await child.process.executeCommand(cmd)).result, "https://example.com"), {
          "https://example.com": true,
        });
      } finally {
        await child.delete();
      }
    } finally {
      await parent.delete();
      // Gone already when the test failed before the snapshot.
      await new Arker().getVm(name).then((snapshot) => snapshot.delete(), () => undefined);
    }
  });

  await section("multi-line envs survive reconnecting and snapshots", async () => {
    const pem = "-----BEGIN KEY-----\nab'c \"d\" $e\n-----END KEY-----\n";
    const withPem = await daytona.create({ envVars: { PEM: pem, PLAIN: "x" } });
    const name = `daytona-pem-${run}`;
    try {
      assert.equal((await withPem.process.executeCommand('printf %s "$PEM"')).result, pem);
      assert.deepEqual((await daytona.get(withPem.id)).env, { PEM: pem, PLAIN: "x" });
      await withPem.createSnapshot(name);
      const child = await daytona.create({ snapshot: name, envVars: { PLAIN: "y" } });
      try {
        assert.deepEqual((await daytona.get(child.id)).env, { PEM: pem, PLAIN: "y" });
        assert.equal((await child.process.executeCommand('printf %s "$PEM"')).result, pem);
      } finally {
        await child.delete();
      }
    } finally {
      await withPem.delete();
      await new Arker().getVm(name).then((snapshot) => snapshot.delete(), () => undefined);
    }
  });
} finally {
  await daytona.delete(sandbox);
}

console.log("PASS daytona lifecycle live");
