import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import * as Native from "e2b";
import * as Shim from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";
import { traceVendor } from "./compat-trace.js";

export async function program(Sandbox: typeof Native.Sandbox): Promise<void> {
  const owned: Native.Sandbox[] = [], errors: unknown[] = [], samples: Set<string>[] = [];
  let pending: Promise<Array<Native.Sandbox | Error>> | undefined;
  const metadata = { fork_case: randomUUID() };
  const path = "/home/user/fork-payload", ready = "/home/user/fork-memory";
  const inventory = async () => {
    const paginator = Sandbox.list({ query: { metadata } });
    const ids = new Set<string>();
    while (paginator.hasNext) for (const row of await paginator.nextItems()) ids.add(row.sandboxId);
    return ids;
  };
  try {
    const parent = await Sandbox.create({ timeoutMs: 300_000, metadata, network: { allowPublicTraffic: false } });
    owned.push(parent);
    const before = await parent.getInfo(), parentToken = parent.trafficAccessToken;
    assert.ok(typeof parentToken === "string" && parentToken.length > 0);
    await parent.files.write(path, "before");
    const process = await parent.commands.run(
      `value=$(cat /proc/sys/kernel/random/uuid); printf %s "$value" > ${ready}; read release; printf "%s:%s" "$value" "$release"`,
      { background: true, stdin: true, timeoutMs: 240_000 },
    );
    let deadline = performance.now() + 30_000;
    while (!await parent.files.exists(ready)) {
      assert.ok(performance.now() < deadline, "Background process did not publish its memory value");
      await delay(50);
    }
    const memory = await parent.files.read(ready);
    process.disconnect();
    const server = await parent.commands.run("python3 -m http.server 8080 --directory /home/user", { background: true, timeoutMs: 240_000 });
    server.disconnect();
    const request = (url: string, token?: string) => fetch(url, {
      headers: token ? { "e2b-traffic-access-token": token } : {}, redirect: "manual", signal: AbortSignal.timeout(60_000),
    });
    deadline = performance.now() + 60_000;
    while (true) {
      const response = await request(`https://${parent.getHost(8080)}/fork-payload`, parentToken);
      const body = await response.text();
      if (response.status === 200) { assert.equal(body, "before"); break; }
      assert.ok(performance.now() < deadline, "Parent application did not become ready");
      await delay(100);
    }
    pending = parent.fork({ count: 2, timeoutMs: 180_000, requestTimeoutMs: 120_000 });
    let settled = false;
    // Both handlers consume rejection while inventory is being sampled.
    void pending.then(() => { settled = true; }, () => { settled = true; });
    while (!settled) { samples.push(await inventory()); await delay(50); }
    const values = await pending;
    owned.push(...values.filter((value): value is Native.Sandbox => !(value instanceof Error)));
    pending = undefined;
    assert.equal(values.length, 2);
    const children = values.map(value => { if (value instanceof Error) throw value; return value; });
    const expected = new Set(owned.map(sandbox => sandbox.sandboxId));
    assert.ok(samples.length > 0, "Inventory was not observed during fork");
    assert.ok(samples.every(ids => [...ids].every(id => expected.has(id))), "Temporary checkpoint leaked into inventory");
    deadline = performance.now() + 30_000;
    while (true) {
      const ids = await inventory();
      if (ids.size === expected.size && [...ids].every(id => expected.has(id))) break;
      assert.ok(performance.now() < deadline, "Inventory did not expose exactly parent and children");
      await delay(100);
    }
    const tokens = owned.map(sandbox => sandbox.trafficAccessToken);
    assert.ok(tokens.every(token => typeof token === "string" && token.length > 0));
    assert.equal(new Set(tokens).size, 3, "Fork must rotate each child's private traffic token");
    await parent.files.write(path, "parent-after");
    for (const [index, child] of children.entries()) {
      assert.deepEqual((await child.getInfo()).metadata, metadata);
      assert.equal(await child.files.read(path), "before");
      const handle = await child.commands.connect(process.pid, { timeoutMs: 30_000 });
      await child.commands.sendStdin(process.pid, "child\n");
      const result = await handle.wait();
      assert.equal(result.stdout, `${memory}:child`);
      assert.equal(result.exitCode, 0);
      const payload = `child-${index}`;
      await child.files.write(path, payload);
      const originalFetch = globalThis.fetch;
      let url: string;
      try {
        globalThis.fetch = () => { throw new Error("URL mint performed I/O"); };
        url = await child.downloadUrl(path);
        assert.equal(typeof url, "string");
      } finally { globalThis.fetch = originalFetch; }
      const response = await request(url);
      assert.equal(response.status, 200);
      assert.equal(await response.text(), payload);
      for (const token of tokens) {
        const response = await request(`https://${child.getHost(8080)}/fork-payload`, token);
        const body = await response.text();
        assert.equal(response.status, token === child.trafficAccessToken ? 200 : 403);
        if (token === child.trafficAccessToken) assert.equal(body, payload);
      }
    }
    assert.equal(await parent.files.read(path), "parent-after");
    const parentHandle = await parent.commands.connect(process.pid, { timeoutMs: 30_000 });
    await parent.commands.sendStdin(process.pid, "parent\n");
    assert.equal((await parentHandle.wait()).stdout, `${memory}:parent`);
    const after = await parent.getInfo();
    assert.equal(+after.startedAt, +before.startedAt);
    assert.equal(+after.endAt, +before.endAt);
    assert.ok(parent.trafficAccessToken === parentToken);
  } catch (error) { errors.push(error); }
  finally {
    if (pending) {
      try {
        for (const value of await pending) {
          if (value instanceof Error) errors.push(value); else owned.push(value);
        }
      } catch (error) { errors.push(error); }
    }
    for (const sandbox of owned.reverse()) {
      try { assert.equal(await sandbox.kill(), true); } catch (error) { errors.push(error); }
    }
  }
  if (errors.length) throw new AggregateError(errors, "Fork differential or cleanup failed");
}

async function main(): Promise<void> {
  const routes = process.env.COMPAT_E2B_ROUTES?.split(",") ?? ["reference", "native", "arker"];
  assert.ok(routes.every(route => ["reference", "native", "arker"].includes(route)));
  await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"], async ({ setProvider }) => {
    assert.ok(process.env.ARKER_SOURCE || process.env.ARKER_SOURCE_VM || process.env.ARKER_SOURCE_VM_ID);
    const key = process.env.E2B_API_KEY, errors: unknown[] = [];
    for (const route of routes) {
      await setProvider(route === "arker" ? "arker" : "e2b");
      if (route === "arker") delete process.env.E2B_API_KEY; else process.env.E2B_API_KEY = key;
      let outcome = "failed";
      const calls = new Set<string>();
      try {
        await program(traceVendor((route === "reference" ? Native.Sandbox : Shim.Sandbox) as typeof Native.Sandbox, "Sandbox.static", calls));
        outcome = "passed";
      } catch (error) { errors.push(error); }
      finally {
        await recordEvidence("e2b", route, "import-only-atomic-fork", [...calls].sort(), outcome);
      }
    }
    if (errors.length) throw new AggregateError(errors, "Fork differential routes failed");
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
