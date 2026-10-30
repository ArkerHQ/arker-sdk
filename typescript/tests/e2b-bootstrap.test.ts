import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { bootstrapEnvd } from "../src/compat/e2b-bootstrap.js";
import { manifest } from "../src/compat/e2b-envd-guest.generated.js";

for (const envs of [undefined, {}, { TEST_VALUE: "synthetic-value" }] as Array<Record<string, string> | undefined>)
for (const verified of [false, true])
for (const existing of [false, true]) test(`bootstrap secret request and existing token ${existing}, verified ${verified}, envs ${JSON.stringify(envs)}`, async () => {
  const cache = await mkdtemp(join(tmpdir(), "envd-bootstrap-"));
  const token = "a".repeat(43), root = "/root/.arker/e2b-envd";
  const payload = Buffer.from("fixture");
  const sha = createHash("sha256").update(payload).digest("hex");
  const entry = manifest.artifacts.amd64 as { sha256: string };
  const original = entry.sha256; entry.sha256 = sha;
  const writes: any[] = [];
  const lifetime = {incarnation: {vm_id: "vm-a", pid: 10, start: "20", boot_id: "boot"}, session_id: "hold"};
  const vm = {
    getRun: async () => ({state:"running", vm_id:"vm-a", session_id:"hold", stdoutBytes: Buffer.from(JSON.stringify({ready:true,lifetime})+"\n")}),
    id: "vm-a", refresh: async () => ({ hostname: "vm-a.example.com" }),
    sync: async (path: string, data?: any, options?: any) => {
      if (data === undefined) return Buffer.from(JSON.stringify({ vm_id: "vm-a", token, traffic_access_token: "b".repeat(43) }));
      writes.push({ path, data, options });
    },
    run: async (command: string, options?: any) => {
      assert.ok(!command.includes(token));
      assert.ok(!command.includes("synthetic-value"));
      if (command.includes(" publish ")) return {exitCode:0, stdout:JSON.stringify({ready:true})};
      if (command.includes(" lifetime ")) return {exitCode:0, stdout:JSON.stringify(lifetime)};
      if (command.includes(" monitor ")) {
        assert.equal(options.timeout, 0); assert.equal(options.session_id, "hold");
        return {type:"background", runId:"held"};
      }
      if (command.includes(" prepare ")) return { exitCode: 0, stdout: JSON.stringify({
        root_path: root, request_directory: root + "/requests", state_path: root + "/state.json",
        existing_state_path: existing ? root + "/state.json" : null, arch: "amd64",
      }) };
      if (command.includes(" verify ")) return { exitCode: 0, stdout: JSON.stringify({ verified, arch: "amd64" }) };
      if (command.includes(" ensure ")) {
        assert.deepEqual(options, { timeout: 120, end_after_all_subprocesses: false });
        return { exitCode: 0, stdout: JSON.stringify({ ready: true, reused: existing, port: 49983, version: "0.10.2" }) };
      }
      return { exitCode: command.startsWith("test ") ? 1 : 0 };
    },
  };
  try {
    await writeFile(join(cache, sha), payload);
    const [descriptor, ready] = await bootstrapEnvd(vm as any, { cacheDirectory: cache, envs });
    assert.equal(descriptor.endpoint, "https://vm-a-49983.example.com");
    if (existing) assert.equal(descriptor.envd_access_token, token);
    assert.equal(ready.ready, true);
    for (const write of writes.filter(w => !w.path.includes("/.candidate-"))) assert.equal(write.options.isSecret, true);
    const request = JSON.parse(writes.at(-1).data);
    assert.equal(request.traffic_access_token, existing ? "b".repeat(43) : null);
    assert.equal(request.token, descriptor.envd_access_token);
    assert.equal(Object.hasOwn(request, "envs"), envs !== undefined);
    if (envs !== undefined) assert.deepEqual(request.envs, envs);
    assert.equal(request.offline, true);
    assert.equal(request.retain_activity, true);
    if (verified) {
      assert.equal(request.artifact_path, null);
      assert.ok(writes.every(write => !write.path.includes("/.candidate-")));
    } else assert.match(request.artifact_path, /\/\.candidate-/);
  } finally { entry.sha256 = original; await rm(cache, { recursive: true }); }
});

for (const [label, rule] of Object.entries({
  public: { action: "allow", auth: "open", match: { ports: [49983] } },
  range: { action: "allow", auth: "open", match: { ports: [[49000, 50000]] } },
  reversedRange: { action: "allow", auth: "open", match: { ports: [[50000, 49000]] } },
  token: { action: "allow", auth: { token: "***" }, match: { ports: [49983] } },
  conditional: { action: "allow", auth: "open", match: { ports: [49983], paths: ["/health"] } },
  rewrite: { action: { rewrite: { path: "/" } }, auth: "open", match: { ports: [49983] } },
})) test(`bootstrap rejects ${label} exposure before guest access despite higher private rule`, async () => {
  const calls: string[] = [];
  const policies = { rules: {
    private: { type: "inbound", priority: 1000000, action: "allow", auth: "arker", match: { ports: [49983] } },
    inherited: { type: "inbound", priority: 1, ...rule },
  } };
  const original = JSON.stringify(policies);
  const vm = {
    policies: { rules: {} },
    refresh: async () => { calls.push("refresh"); return { policies }; },
    sync: async () => { calls.push("sync"); },
    run: async () => { calls.push("run"); },
  };
  await assert.rejects(bootstrapEnvd(vm as any), /owner-authenticated ingress/);
  assert.deepEqual(calls, ["refresh"]);
  assert.equal(JSON.stringify(policies), original);
});

for (const [label, rules] of Object.entries({
  empty: {},
  private: { envd: { type: "inbound", priority: 1, action: "allow", auth: "arker", match: { ports: [49983] } } },
  defaultAuth: { envd: { type: "inbound", priority: 1, action: "allow", match: { ports: [49983] } } },
  publicApps: { apps: { type: "inbound", priority: 1, action: "allow", auth: "open", match: { ports: [[1,49982],[49984,65535]] } } },
})) test(`bootstrap accepts ${label} policy without mutation`, async () => {
  const calls: string[] = [];
  const stop = new Error("guest access reached");
  const vm = {
    refresh: async () => { calls.push("refresh"); return { policies: { rules } }; },
    sync: async () => { calls.push("sync"); throw stop; },
  };
  await assert.rejects(bootstrapEnvd(vm as any), error => error === stop);
  assert.deepEqual(calls, ["refresh", "sync"]);
});

test("reconnect checks newly public policy before accessing an existing daemon", async () => {
  let publicPort = false, guestCalls = 0, reads = 0;
  const stop = new Error("private bootstrap reached guest");
  const vm = {
    refresh: async () => {
      reads++;
      return { policies: { rules: { envd: {
        type: "inbound", priority: 1, action: "allow", auth: publicPort ? "open" : "arker", match: { ports: [49983] },
      } } } };
    },
    sync: async () => { guestCalls++; throw stop; },
  };
  await assert.rejects(bootstrapEnvd(vm as any), error => error === stop);
  publicPort = true;
  await assert.rejects(bootstrapEnvd(vm as any), /owner-authenticated ingress/);
  assert.equal(reads, 2);
  assert.equal(guestCalls, 1);
});
