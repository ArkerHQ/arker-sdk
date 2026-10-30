import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Image } from "modal";
import { Arker, VM, ArkerError, type ForkOptions } from "../src/index.js";
import { ModalClient } from "../src/modal.js";
import { BUILD_POLICY, imageMetadata, preparedSteps, finishPrepared, createPrepared } from "../src/compat/modal-prepared.js";
import { shellQuote } from "../src/compat/common.js";
import { preserveEnvironment } from "./provider-fixtures.js";

function fixture(calls: any[], id = "builder", failure?: string): VM {
  return {
    id, baseUrl: "https://fixture.invalid", _client: {},
    sync: async (path: string, value?: string) => {
      if (value === undefined) return new TextEncoder().encode(JSON.stringify({ env: [["BASE", "kept"]], user: "1000:1000", workdir: "/work" }));
      calls.push(["sync", id, path, value]);
    },
    createSession: async () => ({ session_id: "build-session" }),
    deleteSession: async (session: string) => { calls.push(["deleteSession", session]); },
    run: async (command: string, options: unknown) => { calls.push(["run", command, options]); return { type: "completed", exitCode: 0 }; },
    fork: async (request: unknown) => {
      calls.push(["fork", id, request]);
      if (failure === id) throw new Error("fork failed");
      return fixture(calls, id === "builder" ? "capture" : "child", failure);
    },
    delete: async () => { calls.push(["delete", id]); },
  } as unknown as VM;
}
const empty = () => imageMetadata({ env: [], user: null, workdir: null, cmd: null, entrypoint: null });

for (const entryShell of [false, true]) for (const cmdShell of [false, true]) for (const explicitShell of [false, true]) {
  test(`combined startup preserves OCI argv entryShell=${entryShell} cmdShell=${cmdShell} explicitShell=${explicitShell}`, async () => {
    const directory = mkdtempSync(join(tmpdir(), "prepared-startup-"));
    try {
      const marker = join(directory, "must-not-exist");
      const raw = `echo "$HOME" > ${shellQuote(marker)}`;
      const args = ["extra", "$HOME", ">", marker];
      const entrypoint = entryShell ? 'printf "%s" entry' : JSON.stringify(["printf", "%s\\n"]);
      const commands = [`ENTRYPOINT ${entrypoint}`];
      if (explicitShell) commands.push('SHELL ["/bin/sh", "-ec"]');
      commands.push(`CMD ${cmdShell ? raw : JSON.stringify(args)}`);
      const calls: any[] = [];
      const client = { fork: async () => fixture(calls) } as unknown as Arker;
      await createPrepared(client, { image: "fixture" }, preparedSteps(commands));
      const config = JSON.parse(calls.find(call => call[0] === "sync" && call[2].endsWith("image-config.json"))[3]);
      const stdout = execFileSync("sh", ["-c", `${config.entrypoint} ${config.cmd}`], { encoding: "utf8" });
      const cmdArgv = cmdShell ? ["/bin/sh", explicitShell ? "-ec" : "-c", raw] : args;
      assert.equal(stdout, entryShell ? "entry" : cmdArgv.map(value => value + "\n").join(""));
      assert.equal(existsSync(marker), false);
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}

test("resolved profile keeps shell metacharacters literal", () => {
  const value = "'$(printf WRONG); line\nnext";
  const [encoded, profile] = imageMetadata({ env: [["LITERAL", value]], user: "1000:1000", workdir: "/tmp", cmd: "'true'", entrypoint: null });
  assert.equal(JSON.parse(encoded).env[0][1], value);
  assert.equal(execFileSync("sh", ["-c", profile + '\nprintf "%s" "$LITERAL"'], { encoding: "utf8" }), value);
});
for (const change of [{ env: [["BAD=KEY", "value"]] }, { user: "1000:" }, { workdir: "relative" }, { cmd: "x".repeat(32769) }]) {
  test(`rejects malformed metadata ${Object.keys(change)[0]}`, () => assert.throws(() => imageMetadata({ env: [], user: null, workdir: null, cmd: null, entrypoint: null, ...change } as any)));
}
for (const commands of [["RUN true"], ["ENV VALUE=unsupported", "CMD true", "ENTRYPOINT []"]]) {
  test(`unsupported recipe rejects before allocation: ${commands[0]}`, () => assert.throws(() => preparedSteps(commands)));
}

test("public recipe creation builds privately with separate final lifetime and exact native image identity", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const calls: any[] = [];
  t.mock.method(Arker.prototype, "fork", async (request: ForkOptions) => { calls.push(["builder", request]); return fixture(calls); });
  const client = new ModalClient({ arker: { apiKey: "synthetic", baseUrl: "https://fixture.invalid", policies: { rules: { "customer-egress": { type: "outbound_guard", allow: [] } } } } });
  const app = await client.apps.fromName("prepared", { createIfMissing: true });
  const image = client.images.fromRegistry("example/image").dockerfileCommands(["RUN printf built > /artifact", 'ENTRYPOINT ["/bin/sh", "-c"]', 'CMD ["exec sleep 100"]']);
  assert.ok(image instanceof Image);
  assert.equal(Object.getPrototypeOf(image), Image.prototype);
  await image.build(app);
  const box = await client.sandboxes.create(app, image, { timeoutMs: 10_000 });
  assert.equal(box.sandboxId, "child");
  const builder = calls[0][1];
  assert.deepEqual(builder.image_config, { cmd: [], entrypoint: [] });
  assert.equal(builder.policies.rules["computesdk-modal-lifetime"], undefined);
  assert.equal(builder.policies.rules[BUILD_POLICY].after_created, 900);
  assert.equal(builder.policies.rules["computesdk-modal-build-ingress"].action, "deny");
  assert.equal(builder.policies.secrets, undefined);
  assert.deepEqual(builder.policies.rules["customer-egress"], { type: "outbound_guard", allow: [] });
  assert.ok(calls.find(call => call[0] === "run")[2].timeout > 10);
  const child = calls.find(call => call[0] === "fork" && call[1] === "capture")[2];
  assert.equal(child.start_image, true);
  assert.deepEqual(child.layers, ["disk"]);
  assert.equal(child.policies.rules["computesdk-modal-lifetime"].after_created, 10);
  assert.equal(child.policies.rules[BUILD_POLICY], null);
  assert.deepEqual(child.policies.rules["customer-egress"], { type: "outbound_guard", allow: [] });
  assert.equal(child.policies.rules["computesdk-modal-build-ingress"], null);
  assert.deepEqual(child.policies.rules["computesdk-modal-prepared-ingress"], { type: "inbound", priority: 0, action: "allow", auth: "arker" });
  assert.deepEqual(child.metadata, { "computesdk-modal-app": { name: "prepared" }, "computesdk-modal-image-workdir": "/work" });
  assert.deepEqual(calls.slice(-2), [["delete", "capture"], ["delete", "builder"]]);
});

for (const failure of ["builder", "capture"]) for (const image_config of [undefined, { env: { TOKEN: "synthetic-secret" }, workdir: "/final" }]) {
  test(`fork failure reclaims owned resources at ${failure} overrides=${!!image_config}`, async () => {
    const calls: any[] = [];
    await assert.rejects(finishPrepared(fixture(calls, "builder", failure), empty(), { image_config }), /fork failed/);
    assert.deepEqual(calls.filter(call => call[0] === "delete").map(call => call[1]), failure === "builder" ? ["builder"] : ["capture", "builder"]);
  });
}

for (const image_config of [undefined, { env: { TOKEN: "synthetic-secret" }, workdir: "/final" }]) test(`cancellation after final fork reclaims late child overrides=${!!image_config}`, async () => {
  const calls: any[] = [], controller = new AbortController();
  const builder = fixture(calls);
  const capture = fixture(calls, "capture");
  builder.fork = async () => capture;
  capture.fork = async () => { controller.abort(); return fixture(calls, "child"); };
  await assert.rejects(finishPrepared(builder, empty(), { image_config }, controller.signal), /abort/i);
  assert.deepEqual(calls.filter(call => call[0] === "delete").map(call => call[1]), ["child", "capture", "builder"]);
});

test("typed startup recovery ID is reclaimed without losing primary failure", async t => {
  const calls: any[] = [];
  const builder = fixture(calls);
  const error = new ArkerError("unavailable", "startup failed", 503, {
    code: "unavailable", message: "startup failed", timestamp: "2026-01-01T00:00:00Z", request_id: "test",
    request: { kind: "matched", operation_id: "fork" }, recovery: { work: "unknown", context: { vm_id: "recovery-child" } },
  });
  builder.fork = async () => { throw error; };
  t.mock.method(VM.prototype, "delete", async function (this: VM) { calls.push(["delete", this.id]); return {} as never; });
  await assert.rejects(finishPrepared(builder, empty(), {}), value => value === error);
  assert.deepEqual(calls.filter(call => call[0] === "delete").map(call => call[1]), ["recovery-child", "builder"]);
});

for (const explicit of [undefined, "/explicit"]) {
  test(`reconnected prepared exec preserves image workdir (override=${explicit})`, async t => {
    const { Sandbox } = await import("../src/modal.js");
    const { GuestProcesses } = await import("../src/compat/guest-process.js");
    const calls: any[] = [];
    t.mock.method(GuestProcesses.prototype, "exec", async (options: Parameters<InstanceType<typeof GuestProcesses>["exec"]>[0]) => { calls.push(options); return { pid: "process" } as never; });
    const box = new Sandbox({ sandboxId: "owned", provider: "arker", getInstance: () => ({ metadata: {
      "computesdk-modal-image-workdir": "/image-workdir",
    } }) } as never);
    await box.exec(["pwd"], explicit === undefined ? undefined : { workdir: explicit });
    assert.equal(calls[0].workingDir, explicit ?? "/image-workdir");
    assert.deepEqual(calls[0].env, {});
  });
}

test("prepared startup secrets and overrides reach only the final child", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const calls: any[] = [];
  const fork = t.mock.method(Arker.prototype, "fork", async (request: ForkOptions) => {
    calls.push(["builder_request", request]);
    return fixture(calls);
  });
  const client = new ModalClient({ arker: { apiKey: "synthetic", baseUrl: "https://fixture.invalid" } });
  const app = await client.apps.fromName("prepared", { createIfMissing: true });
  const image = client.images.fromRegistry("example/image").dockerfileCommands(["CMD true", "ENTRYPOINT []"]);
  const secret = await client.secrets.fromObject({ TOKEN: "synthetic-secret", SHARED: "secret" });
  const box = await client.sandboxes.create(app, image, { secrets: [secret], env: { SHARED: "explicit" }, workdir: "/final" });
  assert.equal(box.sandboxId, "child");
  assert.equal(fork.mock.callCount(), 1);
  const child = calls.find(call => call[0] === "fork" && call[1] === "capture")[2];
  assert.deepEqual(JSON.parse(JSON.stringify(child.image_config)), { env: { TOKEN: "synthetic-secret", SHARED: "explicit" }, workdir: "/final" });
  assert.equal(child.metadata["computesdk-modal-image-workdir"], "/final");
  assert.equal(child.metadata["computesdk-modal-defaults"], undefined);
  assert.equal(JSON.stringify(child.metadata).includes("synthetic-secret"), false);
  assert.equal(JSON.stringify(calls.filter(call => !(call[0] === "fork" && call[1] === "capture"))).includes("synthetic-secret"), false);
  assert.deepEqual(calls[0][1].image_config, {cmd: [], entrypoint: []});
  assert.deepEqual(calls.slice(-2), [["delete", "capture"], ["delete", "builder"]]);
});
