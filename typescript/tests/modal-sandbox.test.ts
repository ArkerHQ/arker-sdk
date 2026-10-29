import assert from "node:assert/strict";
import { test } from "node:test";
import {
  Image, Volume, Secret, CloudBucketMount, InvalidError, SandboxService,
  type ModalClient as NativeModalClient, type SandboxExecParams,
} from "modal";
import { ModalClient, Sandbox } from "../src/modal.js";
import { GuestProcesses, type GuestProcessResult } from "../src/compat/guest-process.js";
import { createNativeModalProvider } from "../src/compat/modal-provider.js";

test("Modal sandbox service forwards native lookups, listing and snapshot operations", async (t) => {
  const client = new ModalClient({ cpClient: {} as never });
  const value = { sandboxId: "sb-test" };
  const options = { environment: "test", includeTerminated: true };
  for (const method of ["fromName", "experimentalFromName", "experimentalCreate", "experimentalFromSnapshot"] as const) {
    const args = method === "experimentalFromSnapshot" ? [value, options] : [value, value, options];
    t.mock.method(SandboxService.prototype, method, async function (this: SandboxService, ...received: unknown[]) {
      assert.ok(this instanceof SandboxService);
      assert.deepEqual(received, args);
      return value;
    });
    assert.equal(await Reflect.apply(client.sandboxes[method], client.sandboxes, args), value);
  }
  for (const method of ["list", "experimentalList"] as const) {
    const params = { appId: "ap-test", tags: { purpose: "test" }, environment: "test" };
    t.mock.method(SandboxService.prototype, method, async function* (this: SandboxService, received: unknown) {
      assert.ok(this instanceof SandboxService);
      assert.equal(received, params);
      yield value;
    });
    const items = [];
    for await (const item of client.sandboxes[method](params)) items.push(item);
    assert.deepEqual(items, [value]);
  }
});

test("Modal native Image, Volume, Secret and bucket mount handles survive sandbox creation", async () => {
  const client = new ModalClient({ cpClient: {} as never });
  const secret = await client.secrets.fromObject({ FIXTURE: "synthetic" });
  const base = client.images.fromRegistry("alpine:3.20", secret);
  const image = base.dockerfileCommands(["RUN mkdir /data"], { secrets: [secret] });
  const original = new Volume("vo-test");
  const volume = original.withMountOptions({ readOnly: true }).withMountOptions({ subPath: "/data" });
  const mount = client.cloudBucketMounts.create("test-bucket", { secret, readOnly: true, keyPrefix: "data/" });
  assert.ok(secret instanceof Secret);
  assert.ok(image instanceof Image);
  assert.notEqual(image, base);
  assert.equal(secret.secretId, "");
  assert.ok(mount instanceof CloudBucketMount);
  assert.equal(mount.secret, secret);
  assert.deepEqual(volume._mountOptions, { readOnly: true, subPath: "/data" });
  assert.equal(original._mountOptions.readOnly, false);
  const params = { volumes: { "/data": volume }, secrets: [secret], cloudBucketMounts: { "/bucket": mount } };
  const app = { appId: "ap-test" };
  const instance = { sandboxId: "sb-test" };
  const provider = createNativeModalProvider(() => ({ sandboxes: {
    create: async (owner: unknown, source: unknown, received: unknown) => {
      assert.equal(owner, app);
      assert.equal(source, image);
      assert.equal(received, params);
      return instance;
    },
  } }) as unknown as NativeModalClient);
  const box = await provider.instances.create({ modalApp: app, modalImage: image, modalCreateParams: params });
  assert.equal(box.getInstance(), instance);
});

test("Arker Modal exec rejects native-invalid options before spawning", async (t) => {
  const spawn = t.mock.method(GuestProcesses.prototype, "exec", async () => ({ pid: "test" }) as GuestProcessResult);
  const sandbox = new Sandbox({ sandboxId: "test", provider: "arker" } as never);
  for (const timeoutMs of [0, -1000])
    await assert.rejects(sandbox.exec(["true"], { timeoutMs }), /timeoutMs must be positive/);
  for (const timeoutMs of [1, 1500])
    await assert.rejects(sandbox.exec(["true"], { timeoutMs }), /multiple of 1000ms/);
  for (const options of [{ workdir: "relative" }, { env: { "1INVALID": "fixture" } }])
    await assert.rejects(sandbox.exec(["true"], options), InvalidError);
  assert.equal(spawn.mock.callCount(), 0);
  await sandbox.exec(["true"], { timeoutMs: 1000, workdir: "/data", env: { _VALID_1: "fixture" }, stdout: "ignore", stderr: "ignore" });
  assert.equal(spawn.mock.callCount(), 1);
  assert.equal(spawn.mock.calls[0]!.arguments[0]!.timeout, 1000);
});

test("Modal native exec preserves secrets, PTY, process identity and termination results", async () => {
  const process = {};
  const options: SandboxExecParams = { secrets: [new Secret("st-test")], pty: true, mode: "binary" };
  const native = {
    async exec(argv: string[], received: SandboxExecParams) {
      assert.equal(this, native);
      assert.deepEqual(argv, ["true"]);
      assert.equal(received, options);
      return process;
    },
    async terminate(params?: { wait?: boolean }) { return params?.wait ? 137 : undefined; },
  };
  const sandbox = new Sandbox({ sandboxId: "sb-test", provider: "modal", getInstance: () => native } as never);
  assert.equal(await sandbox.exec(["true"], options as SandboxExecParams & { mode: "binary" }), process);
  assert.equal(await sandbox.terminate({ wait: true }), 137);
  assert.equal(await sandbox.terminate(), undefined);
});


test("Arker Modal exec accepts explicit native defaults without changing the command", async t => {
  const spawn = t.mock.method(GuestProcesses.prototype, "exec", async () => ({ pid: "test" }) as GuestProcessResult);
  const sandbox = new Sandbox({ sandboxId: "test", provider: "arker" } as never);
  await sandbox.exec(["printf", "literal value"]);
  await sandbox.exec(["printf", "literal value"], { pty: false, secrets: [], stdout: "pipe", stderr: "pipe", mode: "text" });
  assert.deepEqual(spawn.mock.calls[1]!.arguments, spawn.mock.calls[0]!.arguments);
  assert.equal(spawn.mock.calls[1]!.arguments[0]!.workingDir, "/");
  assert.deepEqual(spawn.mock.calls[1]!.arguments[0]!.env, {});
  assert.equal(spawn.mock.calls[1]!.arguments[0]!.timeout, undefined);
});

test("Arker Modal exec refuses unsupported PTY and secret values before spawning", async t => {
  const spawn = t.mock.method(GuestProcesses.prototype, "exec", async () => ({ pid: "test" }) as GuestProcessResult);
  const sandbox = new Sandbox({ sandboxId: "test", provider: "arker" } as never);
  for (const options of [{ pty: true }, { pty: null }, { secrets: [new Secret("st-test")] }, { secrets: {} }, { secrets: null }]) {
    await assert.rejects(sandbox.exec(["true"], options as SandboxExecParams & { mode?: "text" }), /not supported by Arker/);
  }
  assert.equal(spawn.mock.callCount(), 0);
});
